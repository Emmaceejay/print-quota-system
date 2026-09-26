"""Shared pytest fixtures.

Every test runs against a throwaway SQLite file and a throwaway settings
file, so nothing here can touch a real deployment.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

TEST_SETTINGS = {
    "database": {"url": "", "echo": False},
    "quota": {
        "default_limit": 100,
        "period_days": 30,
        "default_low_balance_threshold": 10,
        "enforcement": "strict",
        "enforce_group_budget": True,
    },
    "printing": {
        "default_cost_per_page_mono": 1.0,
        "default_cost_per_page_color": 5.0,
        "default_duplex_discount": 0.0,
        "currency": "NGN",
        "real_backend_dir": "",
        "page_log": "",
        "estimator_timeout": 5,
    },
    "alerts": {"enabled": False, "channel": "none", "cooldown_hours": 24,
               "smtp": {"host": "", "port": 25, "from_address": "t@localhost"}},
    "api": {"session_cookie": "printquota_session", "session_max_age": 3600,
            "auth_backend": "local"},
    "logging": {"level": "CRITICAL", "use_journald": False},
    "secret_key": "test-secret-key-for-signing-sessions",
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A fully isolated printquota environment (settings + empty database)."""
    from printquota.core import config as config_module
    from printquota.db import session as db_session

    db_path = tmp_path / "printquota.db"
    backend_dir = tmp_path / "backends"
    backend_dir.mkdir()
    page_log = tmp_path / "page_log"
    page_log.touch()
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()

    settings_data = dict(TEST_SETTINGS)
    settings_data["database"] = {"url": f"sqlite:///{db_path}", "echo": False}
    settings_data["printing"] = dict(TEST_SETTINGS["printing"])
    settings_data["printing"]["real_backend_dir"] = str(backend_dir)
    settings_data["printing"]["page_log"] = str(page_log)
    settings_data["printing"]["spool_dir"] = str(spool_dir)

    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(settings_data))

    for name in list(config_module.ENV_MAP):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PRINTQUOTA_CONFIG", str(settings_path))
    config_module.reset_settings_cache()
    db_session.reset()
    db_session.create_all()

    yield {
        "tmp_path": tmp_path,
        "db_path": db_path,
        "backend_dir": backend_dir,
        "page_log": page_log,
        "spool_dir": spool_dir,
        "settings_path": settings_path,
    }

    db_session.reset()
    config_module.reset_settings_cache()


@pytest.fixture()
def seeded(env):
    """The isolated environment with a small, realistic data set."""
    from printquota.db import session as db_session
    from printquota.db.models import Group, Printer, User

    with db_session.session_scope() as session:
        session.add(Group(name="finance", shared_quota=200, description="Finance dept"))
        session.add(Group(name="unlimited-team", shared_quota=None))
        session.add(
            Printer(
                name="hp-mono",
                real_device_uri="socket://10.0.0.5:9100",
                cost_per_page_mono=2.0,
                cost_per_page_color=0.0,
                supports_duplex=True,
                duplex_discount=0.5,
            )
        )
        session.add(
            Printer(
                name="color-mfp",
                real_device_uri="ipp://10.0.0.6/ipp/print",
                cost_per_page_mono=2.0,
                cost_per_page_color=10.0,
                supports_duplex=True,
            )
        )
        session.add(
            User(
                username="alex",
                display_name="Alex",
                email="alex@example.com",
                group_name="finance",
                quota_limit=100,
                low_balance_threshold=10,
            )
        )
        session.add(
            User(
                username="ada",
                display_name="Ada",
                group_name="finance",
                quota_limit=50,
                low_balance_threshold=5,
            )
        )
        session.add(
            User(
                username="solo",
                display_name="Solo",
                quota_limit=20,
                low_balance_threshold=2,
            )
        )
    return env


@pytest.fixture()
def admin_client(seeded):
    """A FastAPI test client signed in as an administrator."""
    from fastapi.testclient import TestClient

    from printquota.api.auth import hash_password
    from printquota.api.main import create_app
    from printquota.db import session as db_session
    from printquota.db.models import User

    with db_session.session_scope() as session:
        admin = session.get(User, "alex")
        admin.is_admin = True
        admin.password_hash = hash_password("s3cret")
        member = session.get(User, "ada")
        member.password_hash = hash_password("hunter2")

    client = TestClient(create_app())
    response = client.post(
        "/login", data={"username": "alex", "password": "s3cret", "next_url": "/admin"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return client


def utc(days: int = 0, hours: int = 0) -> dt.datetime:
    """Helper: a UTC timestamp offset from now."""
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days, hours=hours)
