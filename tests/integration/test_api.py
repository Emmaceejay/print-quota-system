"""Web app: authentication, self-service portal and admin console."""

from __future__ import annotations

from fastapi.testclient import TestClient

from printquota.api.auth import hash_password, issue_session, read_session, verify_password
from printquota.api.main import create_app
from printquota.db import session as db_session
from printquota.db.models import AdminAuditLog, Group, PrintPolicy, Printer, User


def test_password_hashing_round_trip():
    digest = hash_password("s3cret")
    assert digest != "s3cret"
    assert verify_password("s3cret", digest)
    assert not verify_password("wrong", digest)
    assert not verify_password("s3cret", None)


def test_session_token_round_trip(seeded):
    token = issue_session("ceejay")
    assert read_session(token) == "ceejay"
    assert read_session("not-a-token") is None


def test_anonymous_requests_are_redirected_to_the_login_page(seeded):
    client = TestClient(create_app())
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303 and "/login" in response.headers["location"]


def test_bad_credentials_are_rejected(seeded):
    with db_session.session_scope() as session:
        session.get(User, "ada").password_hash = hash_password("hunter2")
    client = TestClient(create_app())
    response = client.post("/login", data={"username": "ada", "password": "nope", "next_url": "/"})
    assert response.status_code == 401


def test_a_non_admin_cannot_reach_the_console(seeded):
    with db_session.session_scope() as session:
        session.get(User, "ada").password_hash = hash_password("hunter2")
    client = TestClient(create_app())
    client.post("/login", data={"username": "ada", "password": "hunter2", "next_url": "/"})
    assert client.get("/admin").status_code == 403
    assert client.get("/me").status_code == 200


def test_portal_shows_the_users_own_balance(admin_client):
    body = admin_client.get("/me").text
    assert "Ceejay" in body and "Allowance" in body


def test_portal_json_endpoint(admin_client):
    data = admin_client.get("/api/me").json()
    assert data["username"] == "ceejay" and data["group"] == "finance"
    assert data["remaining"] == 100


def test_portal_csv_export(admin_client):
    response = admin_client.get("/me/history.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.text.splitlines()[0].startswith("submitted_at")


def test_dashboard_renders(admin_client):
    body = admin_client.get("/admin").text
    assert "Dashboard" in body and "Pages charged" in body


def test_admin_can_create_and_edit_a_user(admin_client):
    response = admin_client.post(
        "/admin/users/create",
        data={"username": "newbie", "display_name": "New Bie", "email": "n@example.com",
              "group_name": "finance", "quota_limit": "250", "password": "pw"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    with db_session.session_scope() as session:
        user = session.get(User, "newbie")
        assert user is not None and user.quota_limit == 250 and user.group_name == "finance"

    admin_client.post(
        "/admin/users/newbie/update",
        data={"quota_limit": "400", "low_balance_threshold": "20", "email": "",
              "group_name": "", "is_active": "true"},
        follow_redirects=True,
    )
    with db_session.session_scope() as session:
        user = session.get(User, "newbie")
        assert user.quota_limit == 400 and user.group_name is None and user.is_active


def test_duplicate_user_is_rejected_with_a_message(admin_client):
    response = admin_client.post(
        "/admin/users/create", data={"username": "ada", "quota_limit": "10"}, follow_redirects=True
    )
    assert "already exists" in response.text


def test_admin_cannot_remove_their_own_admin_rights(admin_client):
    response = admin_client.post(
        "/admin/users/ceejay/update",
        data={"quota_limit": "100", "low_balance_threshold": "10", "is_active": "true"},
        follow_redirects=True,
    )
    assert "cannot remove your own admin rights" in response.text
    with db_session.session_scope() as session:
        assert session.get(User, "ceejay").is_admin


def test_reset_usage_from_the_console(admin_client):
    with db_session.session_scope() as session:
        session.get(User, "ada").pages_used = 33
    admin_client.post("/admin/users/ada/reset", follow_redirects=True)
    with db_session.session_scope() as session:
        assert session.get(User, "ada").pages_used == 0


def test_group_create_and_budget_update(admin_client):
    admin_client.post(
        "/admin/groups/create", data={"name": "ops", "shared_quota": "300"}, follow_redirects=True
    )
    with db_session.session_scope() as session:
        assert session.get(Group, "ops").shared_quota == 300
    admin_client.post(
        "/admin/groups/ops/update", data={"shared_quota": "", "description": "Operations"},
        follow_redirects=True,
    )
    with db_session.session_scope() as session:
        group = session.get(Group, "ops")
        assert group.shared_quota is None and group.description == "Operations"


def test_printer_upsert_validates_the_duplex_discount(admin_client):
    admin_client.post(
        "/admin/printers/save",
        data={"name": "lab", "real_device_uri": "socket://1.2.3.4:9100",
              "cost_per_page_mono": "1.5", "cost_per_page_color": "6", "duplex_discount": "0.4",
              "supports_duplex": "true", "is_active": "true"},
        follow_redirects=True,
    )
    with db_session.session_scope() as session:
        printer = session.get(Printer, "lab")
        assert printer.cost_per_page_mono == 1.5 and printer.duplex_discount == 0.4

    response = admin_client.post(
        "/admin/printers/save",
        data={"name": "lab", "duplex_discount": "1.5"}, follow_redirects=True,
    )
    assert "Duplex discount" in response.text


def test_policy_create_and_delete_with_validation(admin_client):
    bad = admin_client.post(
        "/admin/policies/create",
        data={"scope_type": "global", "rule_type": "max_pages_per_job", "rule_value": "abc"},
        follow_redirects=True,
    )
    assert "positive integer" in bad.text

    admin_client.post(
        "/admin/policies/create",
        data={"scope_type": "user", "scope_value": "ada", "rule_type": "block_color", "rule_value": "true"},
        follow_redirects=True,
    )
    with db_session.session_scope() as session:
        policy = session.query(PrintPolicy).one()
        policy_id = policy.id
    admin_client.post(f"/admin/policies/{policy_id}/delete", follow_redirects=True)
    with db_session.session_scope() as session:
        assert session.query(PrintPolicy).count() == 0


def test_reports_and_csv_export(admin_client):
    from printquota.policies.engine import JobContext
    from printquota.services.quota import authorize_job

    with db_session.session_scope() as session:
        authorize_job(session, JobContext(username="ada", printer="hp-mono", estimated_pages=4), cups_job_id=9)

    page = admin_client.get("/admin/reports?days=30")
    assert "Usage report" in page.text and "ada" in page.text

    csv_response = admin_client.get("/admin/reports.csv?days=30")
    assert csv_response.status_code == 200
    lines = csv_response.text.strip().splitlines()
    assert lines[0].startswith("submitted_at") and any("ada" in line for line in lines[1:])


def test_every_admin_mutation_is_audited(admin_client):
    admin_client.post("/admin/users/create", data={"username": "zed", "quota_limit": "10"}, follow_redirects=True)
    with db_session.session_scope() as session:
        actions = {row.action for row in session.query(AdminAuditLog).all()}
    assert "user.add" in actions


def test_health_endpoint(seeded):
    client = TestClient(create_app())
    assert client.get("/healthz").json()["status"] == "ok"


def test_logout_clears_the_session(admin_client):
    admin_client.get("/logout", follow_redirects=False)
    assert admin_client.get("/admin", follow_redirects=False).status_code == 303


def test_login_only_redirects_to_relative_targets(seeded):
    with db_session.session_scope() as session:
        session.get(User, "ada").password_hash = hash_password("hunter2")
    client = TestClient(create_app())
    response = client.post(
        "/login",
        data={"username": "ada", "password": "hunter2", "next_url": "https://evil.example/"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/"
