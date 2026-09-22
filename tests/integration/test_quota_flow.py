"""End-to-end quota behaviour against a real (temporary) database."""

from __future__ import annotations

import datetime as dt

from printquota.db import session as db_session
from printquota.db.models import Group, PrintJob, PrintPolicy, User, utcnow
from printquota.policies.engine import JobContext
from printquota.services.quota import (
    authorize_job,
    charge_job,
    ensure_period,
    refund_job,
    reset_expired_periods,
)


def ctx(**kwargs) -> JobContext:
    base = dict(username="ceejay", printer="hp-mono", estimated_pages=10, copies=1)
    base.update(kwargs)
    return JobContext(**base)


def test_allowed_job_debits_the_user_and_the_group_immediately(seeded):
    with db_session.session_scope() as session:
        decision, job = authorize_job(session, ctx(estimated_pages=10))
        assert decision.allowed and job.status == PrintJob.STATUS_ALLOWED
    with db_session.session_scope() as session:
        assert session.get(User, "ceejay").pages_used == 10
        assert session.get(Group, "finance").pages_used == 10


def test_two_jobs_cannot_both_pass_against_the_same_balance(seeded):
    """The pre-flight charge is what stops a burst of jobs overshooting."""
    with db_session.session_scope() as session:
        session.get(User, "solo").quota_limit = 10
    with db_session.session_scope() as session:
        first, _ = authorize_job(session, ctx(username="solo", estimated_pages=8))
    with db_session.session_scope() as session:
        second, _ = authorize_job(session, ctx(username="solo", estimated_pages=8))
    assert first.allowed
    assert not second.allowed and second.rule == "user_quota"


def test_group_pool_blocks_a_member_with_personal_quota_left(seeded):
    with db_session.session_scope() as session:
        session.get(Group, "finance").pages_used = 195
    with db_session.session_scope() as session:
        decision, job = authorize_job(session, ctx(estimated_pages=10))
    assert not decision.allowed and decision.rule == "group_quota"
    assert job.status == PrintJob.STATUS_DENIED
    with db_session.session_scope() as session:
        assert session.get(User, "ceejay").pages_used == 0


def test_a_denied_job_costs_nothing(seeded):
    with db_session.session_scope() as session:
        session.get(User, "ceejay").quota_limit = 1
    with db_session.session_scope() as session:
        _, job = authorize_job(session, ctx(estimated_pages=50))
        assert job.status == PrintJob.STATUS_DENIED and job.cost == 0.0


def test_unknown_and_disabled_users_are_denied(seeded):
    with db_session.session_scope() as session:
        decision, _ = authorize_job(session, ctx(username="ghost"))
    assert not decision.allowed and decision.rule == "unknown_user"

    with db_session.session_scope() as session:
        session.get(User, "ada").is_active = False
    with db_session.session_scope() as session:
        decision, _ = authorize_job(session, ctx(username="ada"))
    assert not decision.allowed


def test_disabled_printer_is_rejected(seeded):
    from printquota.db.models import Printer

    with db_session.session_scope() as session:
        session.get(Printer, "hp-mono").is_active = False
    with db_session.session_scope() as session:
        decision, _ = authorize_job(session, ctx())
    assert not decision.allowed and decision.rule == "printer_inactive"


def test_policy_from_the_database_is_applied(seeded):
    with db_session.session_scope() as session:
        session.add(
            PrintPolicy(scope_type="group", scope_value="finance", rule_type="block_color", rule_value="true")
        )
    with db_session.session_scope() as session:
        decision, _ = authorize_job(session, ctx(printer="color-mfp", is_color=True))
    assert not decision.allowed and decision.rule == "block_color"


def test_reconciliation_adjusts_the_balance_up_and_down(seeded):
    with db_session.session_scope() as session:
        _, job = authorize_job(session, ctx(estimated_pages=10))
        job_id = job.id
    # driver actually imaged 14 pages
    with db_session.session_scope() as session:
        charge_job(session, session.get(PrintJob, job_id), 14)
    with db_session.session_scope() as session:
        assert session.get(User, "ceejay").pages_used == 14
        assert session.get(Group, "finance").pages_used == 14
        job = session.get(PrintJob, job_id)
        assert job.status == PrintJob.STATUS_COMPLETED and job.actual_pages == 14
        assert job.cost == 28.0  # 14 pages x 2.0 mono


def test_reconciliation_is_idempotent(seeded):
    with db_session.session_scope() as session:
        _, job = authorize_job(session, ctx(estimated_pages=10))
        job_id = job.id
    for _ in range(3):
        with db_session.session_scope() as session:
            charge_job(session, session.get(PrintJob, job_id), 14)
    with db_session.session_scope() as session:
        assert session.get(User, "ceejay").pages_used == 14


def test_duplex_discount_is_reflected_in_the_cost(seeded):
    with db_session.session_scope() as session:
        _, job = authorize_job(session, ctx(estimated_pages=10, is_duplex=True))
        job_id = job.id
    with db_session.session_scope() as session:
        charge_job(session, session.get(PrintJob, job_id), 10)
        assert session.get(PrintJob, job_id).cost == 10.0  # 10 x 2.0 x (1 - 0.5)


def test_refund_returns_pages_to_user_and_group(seeded):
    with db_session.session_scope() as session:
        _, job = authorize_job(session, ctx(estimated_pages=25))
        job_id = job.id
    with db_session.session_scope() as session:
        refund_job(session, session.get(PrintJob, job_id), "printer jam")
    with db_session.session_scope() as session:
        assert session.get(User, "ceejay").pages_used == 0
        assert session.get(Group, "finance").pages_used == 0


def test_rolling_period_resets_usage_when_it_elapses(seeded):
    with db_session.session_scope() as session:
        user = session.get(User, "ceejay")
        user.pages_used = 90
        user.period_start = utcnow() - dt.timedelta(days=31)
    with db_session.session_scope() as session:
        decision, _ = authorize_job(session, ctx(estimated_pages=50))
        assert decision.allowed
    with db_session.session_scope() as session:
        assert session.get(User, "ceejay").pages_used == 50


def test_period_anchor_advances_in_whole_windows(seeded):
    anchor = utcnow() - dt.timedelta(days=95)
    with db_session.session_scope() as session:
        user = session.get(User, "ceejay")
        user.period_start = anchor
        user.pages_used = 80
        rolled = ensure_period(session, user, 30)
        assert rolled
        assert user.pages_used == 0
        assert (user.period_start - anchor).days == 90


def test_reset_expired_periods_reports_what_it_rolled(seeded):
    with db_session.session_scope() as session:
        session.get(User, "ceejay").period_start = utcnow() - dt.timedelta(days=40)
        session.get(Group, "finance").period_start = utcnow() - dt.timedelta(days=40)
    with db_session.session_scope() as session:
        result = reset_expired_periods(session)
    assert result["users"] == 1 and result["groups"] == 1
