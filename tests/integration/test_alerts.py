"""Notification dispatch, cooldown and audit trail."""

from __future__ import annotations

import datetime as dt

from printquota.core.config import load_settings, reset_settings_cache
from printquota.db import session as db_session
from printquota.db.models import AlertLog, User, utcnow
from printquota.notifications import alerts
from printquota.services.audit import record_audit


def _enable_webhook(monkeypatch, settings_path, sent):
    import os

    import yaml

    data = yaml.safe_load(settings_path.read_text())
    data["alerts"] = {
        "enabled": True,
        "channel": "webhook",
        "webhook_url": "http://localhost/hook",
        "cooldown_hours": 24,
        "smtp": {"host": "", "port": 25, "from_address": "t@localhost"},
    }
    settings_path.write_text(yaml.safe_dump(data))
    reset_settings_cache()
    monkeypatch.setattr(alerts, "_send_webhook", lambda settings, payload: sent.append(payload) or True)


def test_low_balance_alert_fires_once_within_the_cooldown(seeded, monkeypatch):
    sent: list = []
    _enable_webhook(monkeypatch, seeded["settings_path"], sent)
    with db_session.session_scope() as session:
        session.get(User, "ceejay").pages_used = 95  # 5 left, threshold 10
    with db_session.session_scope() as session:
        assert alerts.notify_balance_state(session, "ceejay") is not None
    with db_session.session_scope() as session:
        assert alerts.notify_balance_state(session, "ceejay") is None
    assert len(sent) == 1 and sent[0]["type"] == AlertLog.TYPE_LOW_BALANCE


def test_over_quota_alert_supersedes_low_balance(seeded, monkeypatch):
    sent: list = []
    _enable_webhook(monkeypatch, seeded["settings_path"], sent)
    with db_session.session_scope() as session:
        session.get(User, "ceejay").pages_used = 100
    with db_session.session_scope() as session:
        alerts.notify_balance_state(session, "ceejay")
    assert sent[0]["type"] == AlertLog.TYPE_OVER_QUOTA


def test_no_alert_while_the_balance_is_healthy(seeded, monkeypatch):
    sent: list = []
    _enable_webhook(monkeypatch, seeded["settings_path"], sent)
    with db_session.session_scope() as session:
        assert alerts.notify_balance_state(session, "ceejay") is None
    assert sent == []


def test_cooldown_expires(seeded, monkeypatch):
    sent: list = []
    _enable_webhook(monkeypatch, seeded["settings_path"], sent)
    with db_session.session_scope() as session:
        session.get(User, "ceejay").pages_used = 95
        session.add(
            AlertLog(
                username="ceejay",
                alert_type=AlertLog.TYPE_LOW_BALANCE,
                sent_at=utcnow() - dt.timedelta(hours=30),
            )
        )
    with db_session.session_scope() as session:
        assert alerts.notify_balance_state(session, "ceejay") is not None


def test_alerts_disabled_means_nothing_is_sent(seeded):
    with db_session.session_scope() as session:
        session.get(User, "ceejay").pages_used = 100
    with db_session.session_scope() as session:
        assert alerts.notify_balance_state(session, "ceejay") is None


def test_a_failing_transport_is_logged_not_raised(seeded, monkeypatch):
    sent: list = []
    _enable_webhook(monkeypatch, seeded["settings_path"], sent)
    monkeypatch.setattr(alerts, "_send_webhook", lambda settings, payload: False)
    with db_session.session_scope() as session:
        session.get(User, "ceejay").pages_used = 100
    with db_session.session_scope() as session:
        entry = alerts.notify_balance_state(session, "ceejay")
        assert entry is not None and entry.delivered is False


def test_audit_entries_serialise_structured_details(seeded):
    with db_session.session_scope() as session:
        entry = record_audit(session, "root", "user.set_quota", "ceejay", {"from": 1, "to": 2})
        session.flush()
        assert '"from": 1' in entry.details
