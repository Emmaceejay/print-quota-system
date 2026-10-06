"""Browser-only administration: setup wizard, users, import, bulk actions,
groups, settings and CUPS queue management."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from printquota.api.auth import verify_password
from printquota.api.main import create_app
from printquota.core import config as config_module
from printquota.core.config import get_settings
from printquota.db import session as db_session
from printquota.db.models import AdminAuditLog, AppSetting, Group, PrintJob, PrintPolicy, Printer, User
from printquota.policies.engine import JobContext
from printquota.services import cups_queues
from printquota.services.quota import authorize_job


def audit_actions() -> set[str]:
    with db_session.session_scope() as session:
        return {row.action for row in session.query(AdminAuditLog).all()}


# ------------------------------------------------------------------ setup wizard
@pytest.fixture()
def setup_token(seeded, monkeypatch):
    monkeypatch.setenv("PRINTQUOTA_SETUP_TOKEN", "tok-123")
    config_module.reset_settings_cache()
    return "tok-123"


def _setup_form(**overrides):
    data = {"token": "tok-123", "username": "boss", "display_name": "The Boss", "email": "boss@example.com",
            "password": "correct-horse", "password_confirm": "correct-horse"}
    data.update(overrides)
    return data


def test_sign_in_page_sends_a_fresh_install_to_setup(setup_token):
    client = TestClient(create_app())
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/setup"
    assert "Setup token" in client.get("/setup").text


def test_setup_requires_the_token(setup_token):
    client = TestClient(create_app())
    response = client.post("/setup", data=_setup_form(token="wrong"))
    assert response.status_code == 403
    with db_session.session_scope() as session:
        assert session.get(User, "boss") is None


def test_setup_without_a_configured_token_is_refused_from_the_network(seeded):
    client = TestClient(create_app())  # the test client is not a loopback address
    assert client.post("/setup", data=_setup_form(token="")).status_code == 403


def test_setup_validates_the_password(setup_token):
    client = TestClient(create_app())
    assert "at least 8" in client.post("/setup", data=_setup_form(password="short", password_confirm="short")).text
    assert "do not match" in client.post("/setup", data=_setup_form(password_confirm="different-pass")).text


def test_setup_creates_an_admin_signs_them_in_and_then_closes(setup_token):
    client = TestClient(create_app())
    response = client.post("/setup", data=_setup_form(), follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/admin")
    assert client.get("/admin").status_code == 200  # signed in by the setup step
    with db_session.session_scope() as session:
        boss = session.get(User, "boss")
        assert boss.is_admin and boss.is_active and verify_password("correct-horse", boss.password_hash)
    assert "setup.admin" in audit_actions()

    stranger = TestClient(create_app())
    assert stranger.get("/setup").status_code == 404
    assert stranger.post("/setup", data=_setup_form(username="mallory")).status_code == 404
    assert stranger.get("/login").status_code == 200


def test_setup_can_promote_an_existing_print_account(setup_token):
    client = TestClient(create_app())
    client.post("/setup", data=_setup_form(username="ada"), follow_redirects=False)
    with db_session.session_scope() as session:
        ada = session.get(User, "ada")
        assert ada.is_admin and ada.quota_limit == 50  # existing quota untouched


# ------------------------------------------------------------------ admins & users
def test_admin_can_create_another_administrator(admin_client):
    admin_client.post("/admin/users/create", data={
        "username": "jane", "quota_limit": "100", "is_admin": "true",
        "password": "jane-password", "password_confirm": "jane-password"})
    with db_session.session_scope() as session:
        jane = session.get(User, "jane")
        assert jane.is_admin and verify_password("jane-password", jane.password_hash)

    other = TestClient(create_app())
    other.post("/login", data={"username": "jane", "password": "jane-password", "next_url": "/admin"})
    assert other.get("/admin").status_code == 200


def test_an_administrator_needs_a_password(admin_client):
    response = admin_client.post("/admin/users/create", data={
        "username": "nopw", "quota_limit": "100", "is_admin": "true"}, follow_redirects=True)
    assert "Set a password" in response.text
    with db_session.session_scope() as session:
        assert session.get(User, "nopw") is None


def test_console_password_rules(admin_client):
    short = admin_client.post("/admin/users/create", data={
        "username": "x1", "quota_limit": "1", "password": "short"}, follow_redirects=True)
    assert "at least 8" in short.text
    mismatch = admin_client.post("/admin/users/ada/update", data={
        "quota_limit": "50", "low_balance_threshold": "5", "is_active": "true",
        "password": "new-password-1", "password_confirm": "new-password-2"}, follow_redirects=True)
    assert "do not match" in mismatch.text


def test_admin_can_reset_a_users_password_and_name(admin_client):
    admin_client.post("/admin/users/ada/update", data={
        "quota_limit": "50", "low_balance_threshold": "5", "is_active": "true", "display_name": "Ada Lovelace",
        "password": "brand-new-pass", "password_confirm": "brand-new-pass"})
    with db_session.session_scope() as session:
        ada = session.get(User, "ada")
        assert ada.display_name == "Ada Lovelace" and verify_password("brand-new-pass", ada.password_hash)


def test_admin_cannot_disable_themselves(admin_client):
    response = admin_client.post("/admin/users/alex/update", data={
        "quota_limit": "100", "low_balance_threshold": "10", "is_admin": "true"}, follow_redirects=True)
    assert "cannot disable your own account" in response.text


def test_user_list_filters(admin_client):
    body = admin_client.get("/admin/users?group=-").text
    assert "solo" in body and "/admin/users/ada" not in body
    body = admin_client.get("/admin/users?status=admin").text
    assert "/admin/users/alex" in body and "/admin/users/solo" not in body


def test_delete_a_user_with_job_history(admin_client):
    with db_session.session_scope() as session:
        authorize_job(session, JobContext(username="ada", printer="hp-mono", estimated_pages=3), cups_job_id=5)
        session.add(PrintPolicy(scope_type="user", scope_value="ada", rule_type="block_color", rule_value="true"))
    refused = admin_client.post("/admin/users/ada/delete", follow_redirects=True)
    assert "I understand" in refused.text
    admin_client.post("/admin/users/ada/delete", data={"confirm": "true"})
    with db_session.session_scope() as session:
        assert session.get(User, "ada") is None
        assert session.query(PrintJob).filter_by(username="ada").count() == 0
        assert session.query(PrintPolicy).count() == 0
    assert admin_client.post("/admin/users/alex/delete", data={"confirm": "true"},
                             follow_redirects=True).text.count("cannot delete your own") == 1


def test_cli_can_delete_a_user_with_job_history(seeded):
    from click.testing import CliRunner

    from printquota.cli.quotactl import cli

    with db_session.session_scope() as session:
        authorize_job(session, JobContext(username="solo", printer="hp-mono", estimated_pages=2), cups_job_id=6)
    result = CliRunner().invoke(cli, ["user", "delete", "solo", "--yes"])
    assert result.exit_code == 0, result.output
    with db_session.session_scope() as session:
        assert session.get(User, "solo") is None


# ------------------------------------------------------------------------- bulk
def test_bulk_move_to_group_and_set_quota(admin_client):
    admin_client.post("/admin/users/bulk", data={
        "usernames": ["ada", "solo"], "action": "set_group", "group_name": "unlimited-team"})
    admin_client.post("/admin/users/bulk", data={
        "usernames": ["ada", "solo"], "action": "set_quota", "quota_limit": "321"})
    with db_session.session_scope() as session:
        for name in ("ada", "solo"):
            user = session.get(User, name)
            assert user.group_name == "unlimited-team" and user.quota_limit == 321
    assert {"user.bulk.set_group", "user.bulk.set_quota"} <= audit_actions()


def test_bulk_actions_never_lock_out_the_acting_admin(admin_client):
    response = admin_client.post("/admin/users/bulk", data={
        "usernames": ["alex", "ada"], "action": "disable"}, follow_redirects=True)
    assert "Your own account was left unchanged" in response.text
    with db_session.session_scope() as session:
        assert session.get(User, "alex").is_active
        assert not session.get(User, "ada").is_active


def test_bulk_delete_needs_confirmation(admin_client):
    admin_client.post("/admin/users/bulk", data={"usernames": ["solo"], "action": "delete"})
    with db_session.session_scope() as session:
        assert session.get(User, "solo") is not None
    admin_client.post("/admin/users/bulk", data={"usernames": ["solo"], "action": "delete", "confirm": "true"})
    with db_session.session_scope() as session:
        assert session.get(User, "solo") is None


def test_bulk_grant_admin(admin_client):
    admin_client.post("/admin/users/bulk", data={"usernames": ["ada"], "action": "make_admin"})
    with db_session.session_scope() as session:
        assert session.get(User, "ada").is_admin


# ----------------------------------------------------------------------- import
CSV = (
    "username,display_name,email,group,quota,password,admin\n"
    "kemi,Kemi A,kemi@example.com,finance,300,kemi-password,no\n"
    "obi,Obi B,obi@example.com,sales,,,\n"
    "ada,Ada Updated,,,,,\n"
    "bad user,,,,,,\n"
    "kemi,dup,,,,,\n"
)


def test_import_preview_writes_nothing(admin_client):
    body = admin_client.post("/admin/users/import", data={"text": CSV, "step": "preview"}).text
    assert "Preview" in body and "does not exist" in body and "more than once" in body
    with db_session.session_scope() as session:
        assert session.get(User, "kemi") is None


def test_import_applies_creates_updates_and_groups(admin_client):
    data = {"text": CSV, "step": "apply", "create_missing_groups": "true",
            "update_existing": "true", "default_quota": "150"}
    response = admin_client.post("/admin/users/import", data=data, follow_redirects=True)
    assert "2 created, 1 updated" in response.text
    with db_session.session_scope() as session:
        kemi = session.get(User, "kemi")
        assert kemi.quota_limit == 300 and kemi.group_name == "finance"
        assert verify_password("kemi-password", kemi.password_hash)
        obi = session.get(User, "obi")
        assert obi.quota_limit == 150 and obi.group_name == "sales"
        assert session.get(Group, "sales") is not None
        ada = session.get(User, "ada")
        assert ada.display_name == "Ada Updated" and ada.quota_limit == 50  # blanks leave fields alone
        assert session.get(User, "bad user") is None
    assert "user.import" in audit_actions()


def test_import_from_an_uploaded_file_with_a_plain_username_list(admin_client):
    files = {"upload": ("staff.csv", b"\xef\xbb\xbfzara\nyusuf\n", "text/csv")}
    admin_client.post("/admin/users/import", data={"step": "apply", "default_quota": "77"}, files=files)
    with db_session.session_scope() as session:
        assert session.get(User, "zara").quota_limit == 77
        assert session.get(User, "yusuf") is not None


def test_import_skips_existing_users_unless_asked(admin_client):
    admin_client.post("/admin/users/import", data={"text": "username,quota\nada,999\n", "step": "apply"})
    with db_session.session_scope() as session:
        assert session.get(User, "ada").quota_limit == 50


def test_import_template_and_export_round_trip(admin_client):
    template = admin_client.get("/admin/users/import/template.csv")
    assert template.status_code == 200 and template.text.startswith("username,")
    export = admin_client.get("/admin/users.csv")
    assert "alex" in export.text and "$2" not in export.text  # never exports password hashes


# ----------------------------------------------------------------------- groups
def test_delete_group_ungroups_members_and_removes_its_policies(admin_client):
    with db_session.session_scope() as session:
        session.add(PrintPolicy(scope_type="group", scope_value="finance", rule_type="block_color", rule_value="true"))
    admin_client.post("/admin/groups/finance/delete")
    with db_session.session_scope() as session:
        assert session.get(Group, "finance") is None
        assert session.get(User, "ada").group_name is None
        assert session.query(PrintPolicy).count() == 0


# --------------------------------------------------------------------- settings
def test_settings_page_saves_and_takes_effect(admin_client):
    assert "Enforcement mode" in admin_client.get("/admin/settings").text
    response = admin_client.post("/admin/settings", data={
        "quota.default_limit": "750", "quota.period_days": "30", "quota.default_low_balance_threshold": "10",
        "quota.enforcement": "soft", "quota.enforce_group_budget": "true",
        "printing.currency": "USD", "printing.default_cost_per_page_mono": "1",
        "printing.default_cost_per_page_color": "5", "printing.default_duplex_discount": "0",
        "alerts.channel": "none", "alerts.cooldown_hours": "24", "alerts.smtp.port": "25",
        "alerts.smtp.password": "",
    }, follow_redirects=True)
    assert "Saved:" in response.text
    settings = get_settings()
    assert settings.get("quota.default_limit") == 750
    assert settings.get("quota.enforcement") == "soft"
    assert settings.get("printing.currency") == "USD"
    assert settings.get("alerts.enabled") is False  # unticked checkbox
    assert "settings.update" in audit_actions()

    # the new enforcement mode reaches the quota engine
    with db_session.session_scope() as session:
        decision, _ = authorize_job(session, JobContext(username="solo", printer="hp-mono", estimated_pages=500))
        assert decision.allowed


def settings_form(admin_client) -> dict:
    """Every field exactly as the Settings page currently shows it."""
    import re

    body = admin_client.get("/admin/settings").text
    form = {}
    for match in re.finditer(r'<input id="([a-z_.]+)" name="\1" type="text"\s+value="([^"]*)"', body):
        form[match.group(1)] = match.group(2)
    for match in re.finditer(r'<select id="([a-z_.]+)" name="\1".*?<option value="([^"]*)" selected', body, re.S):
        form[match.group(1)] = match.group(2)
    for match in re.finditer(r'<input type="checkbox" name="([a-z_.]+)" value="true" checked', body):
        form[match.group(1)] = "true"
    return form


def test_changing_costs_saves_even_if_another_field_is_bad(admin_client):
    """The reported bug: a bad Webhook URL (e.g. browser autofill) blocked a cost change."""
    form = settings_form(admin_client)
    assert form["printing.default_cost_per_page_mono"] == "1"
    form["printing.default_cost_per_page_mono"] = "2.5"
    form["alerts.webhook_url"] = "alex"  # what an autofilled username looks like

    response = admin_client.post("/admin/settings", data=form)
    assert response.status_code == 400
    assert "Saved: Default cost per mono page." in response.text
    assert "is not a web address" in response.text
    assert 'value="alex"' in response.text  # what was typed is kept for correcting
    assert get_settings().get("printing.default_cost_per_page_mono") == 2.5
    assert get_settings().get("alerts.webhook_url") == ""


def test_saving_the_unchanged_form_saves_nothing_and_reports_no_error(admin_client):
    response = admin_client.post("/admin/settings", data=settings_form(admin_client), follow_redirects=True)
    assert "No changes to save" in response.text
    with db_session.session_scope() as session:
        assert session.query(AppSetting).count() == 0


def test_a_questionable_value_from_the_config_file_never_blocks_other_changes(admin_client, seeded):
    import yaml

    data = yaml.safe_load(seeded["settings_path"].read_text())
    data["alerts"]["webhook_url"] = "hooks.internal/printquota"  # no scheme: would fail validation
    seeded["settings_path"].write_text(yaml.safe_dump(data))
    config_module.reset_settings_cache()

    form = settings_form(admin_client)
    assert form["alerts.webhook_url"] == "hooks.internal/printquota"
    form["printing.currency"] = "USD"
    response = admin_client.post("/admin/settings", data=form, follow_redirects=True)
    assert "Saved: Currency label." in response.text
    assert get_settings().get("printing.currency") == "USD"


def test_numbers_are_accepted_the_way_people_type_them(admin_client):
    form = settings_form(admin_client)
    form.update({"quota.default_limit": "1,000", "printing.default_cost_per_page_color": "7,5",
                 "quota.period_days": "31.0"})
    admin_client.post("/admin/settings", data=form)
    settings = get_settings()
    assert settings.get("quota.default_limit") == 1000
    assert settings.get("printing.default_cost_per_page_color") == 7.5
    assert settings.get("quota.period_days") == 31


def test_invalid_values_are_reported_per_field_and_not_saved(admin_client):
    before = get_settings().get("quota.default_limit")
    form = settings_form(admin_client)
    form.update({"quota.default_limit": "lots", "printing.default_duplex_discount": "1.5",
                 "printing.currency": "EUR"})
    response = admin_client.post("/admin/settings", data=form)
    assert response.status_code == 400
    assert "2 settings could not be saved" in response.text
    assert "&#39;lots&#39; is not a number" in response.text and "must be below 1" in response.text
    assert get_settings().get("printing.currency") == "EUR"
    assert get_settings().get("quota.default_limit") == before


def test_environment_pinned_settings_cannot_be_overridden(admin_client, monkeypatch):
    monkeypatch.setenv("PRINTQUOTA_SMTP_HOST", "mail.internal")
    config_module.reset_settings_cache()
    admin_client.post("/admin/settings", data={"alerts.smtp.host": "evil.example"})
    assert get_settings().get("alerts.smtp.host") == "mail.internal"
    assert "set by environment" in admin_client.get("/admin/settings").text


def test_smtp_password_is_kept_when_left_blank_and_never_shown(admin_client):
    admin_client.post("/admin/settings", data={"alerts.smtp.password": "s3cret-smtp"})
    assert get_settings().get("alerts.smtp.password") == "s3cret-smtp"
    admin_client.post("/admin/settings", data={"alerts.smtp.password": ""})
    assert get_settings().get("alerts.smtp.password") == "s3cret-smtp"
    assert "s3cret-smtp" not in admin_client.get("/admin/settings").text
    with db_session.session_scope() as session:
        details = " ".join(row.details or "" for row in session.query(AdminAuditLog).all())
    assert "s3cret-smtp" not in details


def test_revert_a_console_setting(admin_client):
    admin_client.post("/admin/settings", data={"printing.currency": "GBP"})
    assert get_settings().get("printing.currency") == "GBP"
    admin_client.post("/admin/settings/revert", data={"key": "printing.currency"})
    assert get_settings().get("printing.currency") == "NGN"


def test_test_alert_reports_missing_configuration(admin_client):
    response = admin_client.post("/admin/settings/test-alert", follow_redirects=True)
    assert "switched off" in response.text  # the test settings disable alerts


# ------------------------------------------------------------------------ queues
class FakeCups:
    """Stands in for lpstat/lpadmin so queue management runs without CUPS."""

    def __init__(self, queues=None):
        self.queues: dict[str, str] = dict(queues or {})
        self.calls: list[list[str]] = []
        self.forbid = False
        #: queue -> `lpoptions -l` output; a queue without one is a raw queue
        self.ppd_options: dict[str, str] = {}
        #: queue -> options set with `lpadmin -o`
        self.options: dict[str, dict[str, str]] = {}

    def __call__(self, args, timeout):
        self.calls.append(list(args))

        def done(out="", err="", code=0):
            return subprocess.CompletedProcess(args, code, out, err)

        if args[:2] == ["lpstat", "-v"]:
            if not self.queues:
                return done(err="lpstat: No destinations added.", code=1)
            return done("".join(f"device for {n}: {u}\n" for n, u in self.queues.items()))
        if args[:2] == ["lpstat", "-p"]:
            return done("".join(f"printer {n} is idle.  enabled since Mon\n" for n in self.queues))
        if args[0] == "lpadmin":
            if self.forbid:
                return done(err="lpadmin: Forbidden", code=1)
            name = args[args.index("-p") + 1]
            if "-v" in args:
                self.queues[name] = args[args.index("-v") + 1]
            for i, arg in enumerate(args):
                if arg == "-o":
                    key, _, value = args[i + 1].partition("=")
                    self.options.setdefault(name, {})[key] = value
            return done()
        if args[0] == "lpoptions":
            name = args[args.index("-p") + 1]
            if name not in self.ppd_options:
                return done(err=f"lpoptions: Unable to get PPD file for {name}: Not Found", code=1)
            return done(self.ppd_options[name])
        return done(err="unexpected", code=1)


@pytest.fixture()
def fake_cups(admin_client, monkeypatch):
    fake = FakeCups({"hp-mono": "socket://10.0.0.5:9100", "front-desk": "ipp://10.0.0.9/ipp/print"})
    monkeypatch.setattr(cups_queues, "runner", fake)
    # the wrapper backend must exist before a queue can be enforced
    (Path(get_settings().get("printing.real_backend_dir")) / "quota").write_text("#!/bin/sh\n")
    return fake


def test_printers_page_lists_cups_queues(admin_client, fake_cups):
    body = admin_client.get("/admin/printers").text
    assert "front-desk" in body and "Turn on" in body and "color-mfp" in body  # color-mfp: registered, not in CUPS


def test_enforce_registers_the_printer_then_wraps_the_queue(admin_client, fake_cups):
    admin_client.post("/admin/printers/front-desk/enforce")
    assert fake_cups.queues["front-desk"] == "quota:ipp://10.0.0.9/ipp/print"
    with db_session.session_scope() as session:
        printer = session.get(Printer, "front-desk")
        assert printer is not None and printer.real_device_uri == "ipp://10.0.0.9/ipp/print"
    assert "queue.enforce" in audit_actions()


def test_release_needs_confirmation_and_restores_the_uri(admin_client, fake_cups):
    fake_cups.queues["hp-mono"] = "quota:socket://10.0.0.5:9100"
    admin_client.post("/admin/printers/hp-mono/release")
    assert fake_cups.queues["hp-mono"].startswith("quota:")
    admin_client.post("/admin/printers/hp-mono/release", data={"confirm": "true"})
    assert fake_cups.queues["hp-mono"] == "socket://10.0.0.5:9100"


def test_permission_problem_is_explained_and_rolls_back(admin_client, fake_cups):
    fake_cups.forbid = True
    response = admin_client.post("/admin/printers/front-desk/enforce", follow_redirects=True)
    assert "lpadmin" in response.text and "group" in response.text
    with db_session.session_scope() as session:
        assert session.get(Printer, "front-desk") is None


def test_add_a_new_queue_with_enforcement(admin_client, fake_cups):
    admin_client.post("/admin/printers/add-queue", data={
        "name": "lab-mfp", "device_uri": "ipp://10.0.0.20/ipp/print", "driver": "everywhere",
        "cost_per_page_mono": "2", "cost_per_page_color": "9", "duplex_discount": "0.5",
        "supports_duplex": "true", "enforce": "true"})
    assert fake_cups.queues["lab-mfp"] == "quota:ipp://10.0.0.20/ipp/print"
    create_call = next(c for c in fake_cups.calls if c[0] == "lpadmin" and "-m" in c)
    assert create_call[create_call.index("-m") + 1] == "everywhere" and "-E" in create_call
    with db_session.session_scope() as session:
        printer = session.get(Printer, "lab-mfp")
        assert printer.cost_per_page_color == 9 and printer.duplex_discount == 0.5


@pytest.mark.parametrize("name,uri", [
    ("-evil", "ipp://x/ipp"), ("has space", "ipp://x/ipp"), ("ok", "-h"), ("ok", "quota:socket://x"),
])
def test_queue_input_is_validated_before_cups_is_called(admin_client, fake_cups, name, uri):
    admin_client.post("/admin/printers/add-queue", data={"name": name, "device_uri": uri})
    assert not any(c[0] == "lpadmin" for c in fake_cups.calls)


def test_missing_cups_tools_do_not_break_the_page(admin_client, monkeypatch):
    def broken(args, timeout):
        raise cups_queues.CupsError("'lpstat' was not found.")

    monkeypatch.setattr(cups_queues, "runner", broken)
    body = admin_client.get("/admin/printers").text
    assert "Could not read the CUPS queues" in body and "hp-mono" in body


def test_dashboard_shows_the_getting_started_checklist(admin_client, fake_cups):
    body = admin_client.get("/admin").text
    assert "Getting started" in body and "Turn on quota enforcement" in body


def test_admin_json_endpoint_answers_401_in_json(seeded):
    response = TestClient(create_app()).get("/admin/api/summary", follow_redirects=False)
    assert response.status_code == 401 and response.json()["detail"] == "not signed in"


# --------------------------------------------------------------------- two-sided
EVERYWHERE_PPD = (
    "PageSize/Media Size: *A4 Letter Legal\n"
    "Duplex/2-Sided Printing: *None DuplexNoTumble DuplexTumble\n"
    "ColorModel/Output Mode: *Gray RGB\n"
)


def printer_form(name, **overrides):
    form = {"name": name, "cost_per_page_mono": "1", "cost_per_page_color": "5", "duplex_discount": "0.5",
            "supports_duplex": "true", "is_active": "true"}
    form.update(overrides)
    return form


def register(name):
    with db_session.session_scope() as session:
        if session.get(Printer, name) is None:  # hp-mono is seeded
            session.add(Printer(name=name, cost_per_page_mono=1, cost_per_page_color=5))


def test_turning_on_two_sided_sets_the_cups_queue_default(admin_client, fake_cups):
    register("front-desk")
    fake_cups.ppd_options["front-desk"] = EVERYWHERE_PPD
    body = admin_client.post("/admin/printers/save", data=printer_form("front-desk", duplex_default="true"),
                             follow_redirects=True).text
    assert "prints on both sides by default" in body
    assert fake_cups.options["front-desk"] == {"sides-default": "two-sided-long-edge", "Duplex": "DuplexNoTumble"}
    with db_session.session_scope() as session:
        printer = session.get(Printer, "front-desk")
        assert printer.duplex_default and printer.supports_duplex
    assert "printer.duplex_default" in audit_actions()

    admin_client.post("/admin/printers/save", data=printer_form("front-desk"))
    assert fake_cups.options["front-desk"] == {"sides-default": "one-sided", "Duplex": "None"}
    with db_session.session_scope() as session:
        assert not session.get(Printer, "front-desk").duplex_default


def test_saving_costs_alone_does_not_touch_cups(admin_client, fake_cups):
    register("front-desk")
    admin_client.post("/admin/printers/save", data=printer_form("front-desk", cost_per_page_mono="3"))
    assert not any(c[0] == "lpadmin" for c in fake_cups.calls)


def test_a_vendor_driver_gets_its_duplex_unit_marked_installed(admin_client, fake_cups):
    register("hp-mono")
    fake_cups.ppd_options["hp-mono"] = (
        "HPOption_Duplexer/Duplex Unit: *False True\n"
        "Duplex/Print on both sides: *None DuplexNoTumble DuplexTumble\n"
    )
    admin_client.post("/admin/printers/save", data=printer_form("hp-mono", duplex_default="true"))
    assert fake_cups.options["hp-mono"]["HPOption_Duplexer"] == "True"
    assert fake_cups.options["hp-mono"]["Duplex"] == "DuplexNoTumble"


def test_a_raw_queue_is_set_but_the_admin_is_told_why_it_may_not_work(admin_client, fake_cups):
    register("hp-mono")
    body = admin_client.post("/admin/printers/save", data=printer_form("hp-mono", duplex_default="true"),
                             follow_redirects=True).text
    assert fake_cups.options["hp-mono"] == {"sides-default": "two-sided-long-edge"}
    assert "raw queue" in body


def test_a_driver_without_two_sided_is_reported(admin_client, fake_cups):
    register("hp-mono")
    fake_cups.ppd_options["hp-mono"] = "PageSize/Media Size: *A4 Letter\n"
    body = admin_client.post("/admin/printers/save", data=printer_form("hp-mono", duplex_default="true"),
                             follow_redirects=True).text
    assert "no two-sided option" in body


def test_cups_refusing_two_sided_keeps_the_cost_change(admin_client, fake_cups):
    register("front-desk")
    fake_cups.forbid = True
    body = admin_client.post("/admin/printers/save",
                             data=printer_form("front-desk", cost_per_page_mono="4", duplex_default="true"),
                             follow_redirects=True).text
    assert "Could not make front-desk print two-sided" in body
    with db_session.session_scope() as session:
        printer = session.get(Printer, "front-desk")
        assert printer.cost_per_page_mono == 4 and not printer.duplex_default


def test_a_new_queue_can_be_two_sided_from_the_start(admin_client, fake_cups):
    fake_cups.ppd_options["lab-mfp"] = EVERYWHERE_PPD
    body = admin_client.post("/admin/printers/add-queue", data={
        "name": "lab-mfp", "device_uri": "ipp://10.0.0.20/ipp/print", "driver": "everywhere",
        "duplex_default": "true", "enforce": "true"}, follow_redirects=True).text
    assert "prints on both sides by default" in body
    assert fake_cups.queues["lab-mfp"] == "quota:ipp://10.0.0.20/ipp/print"
    assert fake_cups.options["lab-mfp"]["sides-default"] == "two-sided-long-edge"
    with db_session.session_scope() as session:
        assert session.get(Printer, "lab-mfp").duplex_default

