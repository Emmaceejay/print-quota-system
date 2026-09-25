"""Matching the name a job or sign-in arrives with to a print account.

Reproduces the reported case: a domain PC sent ``CORP\\J.Doe`` while the
account is ``j.doe``, and every job was refused.
"""

from __future__ import annotations

import os

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from printquota.api.auth import hash_password
from printquota.api.main import create_app
from printquota.backend import quota_backend as qb
from printquota.cli.quotactl import cli
from printquota.db import session as db_session
from printquota.db.models import PrintJob, PrintPolicy, User
from printquota.policies.engine import JobContext
from printquota.services.identity import name_forms, new_account_problem, resolve_user
from printquota.services.quota import authorize_job


@pytest.fixture()
def jdoe(seeded):
    with db_session.session_scope() as session:
        session.add(User(username="j.doe", display_name="Jane Doe", group_name="finance",
                         quota_limit=10, low_balance_threshold=1))
    return seeded


# ------------------------------------------------------------------- resolution
def test_name_forms():
    assert [f for f, _ in name_forms(r"CORP\J.Doe")] == [r"CORP\J.Doe", "J.Doe"]
    assert [f for f, _ in name_forms("j.doe@corp.example.com")] == ["j.doe@corp.example.com", "j.doe"]
    assert [f for f, _ in name_forms(r"CORP\jdoe@corp.example.com")] == [r"CORP\jdoe@corp.example.com", "jdoe@corp.example.com", "jdoe"]
    assert name_forms("   ") == []


@pytest.mark.parametrize("sent,how", [
    ("j.doe", "exact"),
    ("J.Doe", "case-insensitive"),
    ("J.DOE", "case-insensitive"),
    (r"CORP\J.Doe", "domain-stripped"),
    (r"corp\j.doe", "domain-stripped"),
    ("j.doe@corp.example.com", "realm-stripped"),
    ("  j.doe  ", "exact"),
])
def test_every_form_of_the_name_finds_the_account(jdoe, sent, how):
    with db_session.session_scope() as session:
        match = resolve_user(session, sent)
        assert match.user.username == "j.doe" and match.how == how


def test_unknown_names_find_nothing(jdoe):
    with db_session.session_scope() as session:
        assert resolve_user(session, r"CORP\someone.else").user is None
        assert resolve_user(session, "").user is None


def test_an_exact_account_always_wins(jdoe):
    """A workaround account created before this fix keeps matching exactly."""
    with db_session.session_scope() as session:
        session.add(User(username=r"CORP\J.Doe", quota_limit=5))
    with db_session.session_scope() as session:
        assert resolve_user(session, r"CORP\J.Doe").user.username == r"CORP\J.Doe"
        assert resolve_user(session, "J.Doe").user.username == "j.doe"


def test_accounts_differing_only_in_capitals_are_never_guessed(jdoe):
    with db_session.session_scope() as session:
        session.add(User(username="J.Doe", quota_limit=5))  # bypasses the console's check
    with db_session.session_scope() as session:
        match = resolve_user(session, "J.DOE")
        assert match.ambiguous and match.user is None
        assert match.candidates == ["J.Doe", "j.doe"]


# ----------------------------------------------------------------- print jobs
def test_a_domain_prefixed_job_is_charged_to_the_account(jdoe):
    with db_session.session_scope() as session:
        decision, job = authorize_job(
            session, JobContext(username=r"CORP\J.Doe", printer="hp-mono", estimated_pages=3), cups_job_id=28)
        assert decision.allowed
        assert job.username == "j.doe"
    with db_session.session_scope() as session:
        assert session.get(User, "j.doe").pages_used == 3
        assert session.query(PrintJob).one().username == "j.doe"


def test_user_policies_follow_the_matched_account(jdoe):
    with db_session.session_scope() as session:
        session.add(PrintPolicy(scope_type="user", scope_value="j.doe",
                                rule_type="max_pages_per_job", rule_value="2"))
    with db_session.session_scope() as session:
        decision, _ = authorize_job(
            session, JobContext(username=r"CORP\J.Doe", printer="hp-mono", estimated_pages=3))
        assert not decision.allowed and decision.rule == "max_pages_per_job"


def test_an_ambiguous_name_is_refused_with_a_clear_reason(jdoe):
    with db_session.session_scope() as session:
        session.add(User(username="J.Doe", quota_limit=5))
    with db_session.session_scope() as session:
        decision, _ = authorize_job(
            session, JobContext(username=r"CORP\J.DOE", printer="hp-mono", estimated_pages=1))
        assert not decision.allowed
        assert "matches more than one print account" in decision.reason
        assert "J.Doe, j.doe" in decision.reason


def test_the_backend_accepts_a_domain_prefixed_user(jdoe, monkeypatch):
    """Driven exactly as CUPS drives it, with the name the Windows PC sent."""
    received = jdoe["tmp_path"] / "job.txt"
    received.write_text("line\n" * 100)  # 2 pages
    monkeypatch.setattr(qb, "_stdin_to_tempfile", lambda: received)
    rc = qb.run(["quota", "28", r"CORP\J.Doe", "Budget.xlsx", "1", ""],
                {**os.environ, "PRINTER": "hp-mono", "DEVICE_URI": "quota:socket://10.0.0.5:9100"})
    assert rc != qb.CUPS_BACKEND_CANCEL  # not refused (handing off to the printer is not under test)
    with db_session.session_scope() as session:
        assert session.get(User, "j.doe").pages_used == 2


# -------------------------------------------------------------------- sign-in
@pytest.mark.parametrize("typed", ["j.doe", "J.Doe", r"CORP\J.Doe"])
def test_sign_in_accepts_every_form_of_the_name(jdoe, typed):
    with db_session.session_scope() as session:
        session.get(User, "j.doe").password_hash = hash_password("jdoe-password")
    client = TestClient(create_app())
    response = client.post("/login", data={"username": typed, "password": "jdoe-password", "next_url": "/"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/api/me").json()["username"] == "j.doe"


# ----------------------------------------------------------- account creation
def test_new_account_rules(jdoe):
    with db_session.session_scope() as session:
        assert "without the domain" in new_account_problem(session, r"CORP\J.Doe")
        assert "same account as the existing 'j.doe'" in new_account_problem(session, "J.Doe")
        assert "already exists" in new_account_problem(session, "j.doe")
        assert "no spaces" in new_account_problem(session, "jdoe doe")
        assert new_account_problem(session, "ada.obi") is None


def test_console_refuses_near_duplicates_and_domain_names(jdoe, admin_client):
    near = admin_client.post("/admin/users/create", data={"username": "J.Doe", "quota_limit": "5"},
                             follow_redirects=True)
    assert "same account as the existing" in near.text
    domain = admin_client.post("/admin/users/create", data={"username": r"CORP\x.y", "quota_limit": "5"},
                               follow_redirects=True)
    assert "without the domain" in domain.text
    with db_session.session_scope() as session:
        assert session.query(User).filter(User.username.in_(["J.Doe", r"CORP\x.y"])).count() == 0


def test_cli_refuses_near_duplicates(jdoe):
    result = CliRunner().invoke(cli, ["user", "add", "J.DOE"])
    assert result.exit_code != 0 and "same account" in result.output


def test_import_treats_a_capitalised_name_as_the_existing_account(jdoe, admin_client):
    csv = "username,quota\nJ.Doe,40\nTG\\new.person,10\n"
    preview = admin_client.post("/admin/users/import", data={"text": csv, "step": "preview",
                                                            "update_existing": "true"}).text
    assert "same account as &#39;j.doe&#39;" in preview
    assert "without the domain" in preview
    admin_client.post("/admin/users/import", data={"text": csv, "step": "apply", "update_existing": "true"})
    with db_session.session_scope() as session:
        assert session.get(User, "j.doe").quota_limit == 40
        assert session.get(User, "J.Doe") is None


def test_setup_promotes_an_existing_account_whatever_the_capitals(jdoe, monkeypatch):
    from printquota.core import config as config_module

    monkeypatch.setenv("PRINTQUOTA_SETUP_TOKEN", "tok")
    config_module.reset_settings_cache()
    client = TestClient(create_app())
    client.post("/setup", data={"token": "tok", "username": r"CORP\J.Doe", "password": "jdoe-password",
                                "password_confirm": "jdoe-password"})
    with db_session.session_scope() as session:
        assert session.get(User, "j.doe").is_admin
        assert session.query(User).count() == 4  # nobody new was created


def test_a_legacy_backslash_account_can_still_be_opened_in_the_console(jdoe, admin_client):
    with db_session.session_scope() as session:
        session.add(User(username=r"CORP\Legacy.User", quota_limit=5))
    users = admin_client.get("/admin/users").text
    assert "/admin/users/TG%5CLegacy.User" in users
    page = admin_client.get("/admin/users/TG%5CLegacy.User")
    assert page.status_code == 200 and r"CORP\Legacy.User" in page.text
