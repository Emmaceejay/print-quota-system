"""The quotactl admin CLI."""

from __future__ import annotations

import csv
import datetime as dt
import io

import pytest
from click.testing import CliRunner

from printquota.cli.quotactl import cli
from printquota.db import session as db_session
from printquota.db.models import AdminAuditLog, Group, PrintPolicy, Printer, User, utcnow


@pytest.fixture()
def run(seeded, monkeypatch):
    """Invoke quotactl against the isolated test environment."""
    runner = CliRunner()

    def _run(*args, expect_success: bool = True):
        result = runner.invoke(cli, ["--config", str(seeded["settings_path"]), *args])
        if expect_success:
            assert result.exit_code == 0, result.output + str(result.exception)
        return result

    return _run


def test_user_add_list_and_show(run):
    run("user", "add", "newbie", "--quota", "120", "--group", "finance", "--email", "n@example.com")
    listing = run("user", "list").output
    assert "newbie" in listing and "finance" in listing
    detail = run("user", "show", "newbie").output
    assert "0/120 pages" in detail


def test_user_add_rejects_duplicates_and_unknown_groups(run):
    assert "already exists" in run("user", "add", "ada", expect_success=False).output
    assert "does not exist" in run("user", "add", "x", "--group", "nope", expect_success=False).output


def test_set_quota_and_audit_trail(run):
    run("user", "set-quota", "ada", "999")
    with db_session.session_scope() as session:
        assert session.get(User, "ada").quota_limit == 999
        actions = [row.action for row in session.query(AdminAuditLog).all()]
    assert "user.set_quota" in actions
    assert "user.set_quota" in run("audit").output


def test_disable_and_enable_a_user(run):
    run("user", "disable", "ada")
    with db_session.session_scope() as session:
        assert not session.get(User, "ada").is_active
    run("user", "enable", "ada")
    with db_session.session_scope() as session:
        assert session.get(User, "ada").is_active


def test_reset_clears_usage(run):
    with db_session.session_scope() as session:
        session.get(User, "ada").pages_used = 40
    assert "was 40 pages" in run("user", "reset", "ada").output
    with db_session.session_scope() as session:
        assert session.get(User, "ada").pages_used == 0


def test_set_group_and_clear_it(run):
    run("user", "set-group", "solo", "finance")
    with db_session.session_scope() as session:
        assert session.get(User, "solo").group_name == "finance"
    run("user", "set-group", "solo", "-")
    with db_session.session_scope() as session:
        assert session.get(User, "solo").group_name is None


def test_group_commands(run):
    run("group", "add", "ops", "--budget", "300")
    assert "ops" in run("group", "list").output
    run("group", "set-budget", "ops", "none")
    with db_session.session_scope() as session:
        assert session.get(Group, "ops").shared_quota is None
    with db_session.session_scope() as session:
        session.get(Group, "finance").pages_used = 50
    run("group", "reset", "finance")
    with db_session.session_scope() as session:
        assert session.get(Group, "finance").pages_used == 0


def test_printer_sides_sets_the_cups_queue_default(run, monkeypatch):
    import subprocess

    from printquota.services import cups_queues

    calls = []

    def fake(args, timeout):
        calls.append(list(args))
        if args[:2] == ["lpstat", "-v"]:
            return subprocess.CompletedProcess(args, 0, "device for hp-mono: quota:socket://10.0.0.5:9100\n", "")
        if args[0] == "lpoptions":
            return subprocess.CompletedProcess(args, 0, "Duplex/2-Sided: *None DuplexNoTumble DuplexTumble\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(cups_queues, "runner", fake)
    assert "two-sided" in run("printer", "sides", "hp-mono", "two-sided").output
    assert ["lpadmin", "-p", "hp-mono", "-o", "sides-default=two-sided-long-edge",
            "-o", "Duplex=DuplexNoTumble"] in calls
    with db_session.session_scope() as session:
        assert session.get(Printer, "hp-mono").duplex_default
    assert "two-sided" in run("printer", "list").output
    assert "no such printer" in run("printer", "sides", "nope", "two-sided", expect_success=False).output


def test_printer_commands_and_cost_validation(run):
    run("printer", "add", "lab", "--device-uri", "socket://1.2.3.4:9100", "--mono", "1.5", "--duplex")
    assert "lab" in run("printer", "list").output
    run("printer", "set-cost", "lab", "--color", "8")
    with db_session.session_scope() as session:
        assert session.get(Printer, "lab").cost_per_page_color == 8
    assert "duplex discount" in run(
        "printer", "add", "bad", "--duplex-discount", "1.2", expect_success=False
    ).output
    run("printer", "disable", "lab")
    with db_session.session_scope() as session:
        assert not session.get(Printer, "lab").is_active


def test_policy_add_validates_and_lists(run):
    assert "positive integer" in run(
        "policy", "add", "--scope", "global", "--rule", "max_pages_per_job", "--rule-value", "x",
        expect_success=False,
    ).output
    assert "no such user" in run(
        "policy", "add", "--scope", "user", "--value", "ghost", "--rule", "block_color",
        expect_success=False,
    ).output
    run("policy", "add", "--scope", "user", "--value", "ada", "--rule", "block_color", "--rule-value", "true")
    assert "block_color" in run("policy", "list").output
    with db_session.session_scope() as session:
        policy_id = session.query(PrintPolicy).one().id
    run("policy", "remove", str(policy_id))
    with db_session.session_scope() as session:
        assert session.query(PrintPolicy).count() == 0


def test_usage_report_and_csv_export(run, tmp_path):
    from printquota.policies.engine import JobContext
    from printquota.services.quota import authorize_job

    with db_session.session_scope() as session:
        authorize_job(session, JobContext(username="ada", printer="hp-mono", estimated_pages=6))

    assert "ada" in run("usage", "--days", "7").output

    target = tmp_path / "usage.csv"
    run("usage", "--days", "7", "--csv", str(target))
    rows = list(csv.DictReader(io.StringIO(target.read_text())))
    assert rows and rows[0]["username"] == "ada" and rows[0]["charged_pages"] == "6"


def test_reset_periods_dry_run_and_apply(run):
    with db_session.session_scope() as session:
        session.get(User, "ada").period_start = utcnow() - dt.timedelta(days=45)
        session.get(User, "ada").pages_used = 30
    assert "ada" in run("reset-periods", "--dry-run").output
    with db_session.session_scope() as session:
        assert session.get(User, "ada").pages_used == 30, "dry run must not change anything"
    run("reset-periods")
    with db_session.session_scope() as session:
        assert session.get(User, "ada").pages_used == 0


def test_db_stats(run):
    output = run("db", "stats").output
    assert "users" in output and "printers" in output


def test_db_init_creates_an_admin(seeded, tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--config", str(seeded["settings_path"]), "db", "init", "--admin", "root", "--admin-password", "pw"],
    )
    assert result.exit_code == 0, result.output
    with db_session.session_scope() as session:
        root = session.get(User, "root")
        assert root is not None and root.is_admin and root.password_hash
