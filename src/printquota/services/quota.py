"""Quota lifecycle: period rolling, authorization and charging.

This is the only module that mutates balances. Keeping it in one place is
what makes the invariant ("a job is charged exactly once, against the period
it was submitted in") auditable.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounting.cost import PrinterRates, job_cost
from ..core.config import Settings, get_settings
from ..core.logging import get_logger
from ..db.models import Group, PrintJob, PrintPolicy, Printer, User, utcnow
from ..policies.engine import Decision, JobContext, PolicyRule, QuotaState, evaluate
from .identity import resolve_user

log = get_logger("services.quota")


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def ensure_period(session: Session, entity, period_days: int, now: Optional[dt.datetime] = None) -> bool:
    """Roll a rolling-window period forward if it has elapsed.

    Returns ``True`` when the period was rolled (and ``pages_used`` zeroed).
    Multiple elapsed periods are collapsed by advancing in whole windows, so
    a user who did not print for three months starts a clean window aligned
    to their original anchor date rather than to "now".
    """
    now = now or utcnow()
    start = _aware(entity.period_start)
    if now < start + dt.timedelta(days=period_days):
        return False
    elapsed = (now - start).days
    windows = max(1, elapsed // period_days)
    entity.period_start = start + dt.timedelta(days=period_days * windows)
    entity.pages_used = 0
    session.add(entity)
    return True


def reset_user(session: Session, user: User, now: Optional[dt.datetime] = None) -> None:
    """Force a period reset for one user (admin action)."""
    user.pages_used = 0
    user.period_start = now or utcnow()
    session.add(user)


def reset_expired_periods(
    session: Session, period_days: Optional[int] = None, now: Optional[dt.datetime] = None
) -> dict[str, int]:
    """Roll every elapsed user and group period. Used by the systemd timer."""
    settings = get_settings()
    period_days = period_days or int(settings.get("quota.period_days", 30))
    now = now or utcnow()
    users = session.scalars(select(User)).all()
    groups = session.scalars(select(Group)).all()
    rolled_users = sum(1 for u in users if ensure_period(session, u, period_days, now))
    rolled_groups = sum(1 for g in groups if ensure_period(session, g, period_days, now))
    return {"users": rolled_users, "groups": rolled_groups}


def load_rules(session: Session, ctx: JobContext) -> list[PolicyRule]:
    """Fetch the active policy rows that could apply to this job."""
    stmt = select(PrintPolicy).where(PrintPolicy.is_active.is_(True))
    rows = session.scalars(stmt).all()
    return [PolicyRule.from_orm(row) for row in rows]


def load_quota_state(session: Session, user: User, settings: Optional[Settings] = None) -> QuotaState:
    """Build a :class:`QuotaState` for a user, rolling stale periods first."""
    settings = settings or get_settings()
    period_days = int(settings.get("quota.period_days", 30))
    ensure_period(session, user, period_days)
    group: Optional[Group] = None
    if user.group_name:
        group = session.get(Group, user.group_name)
        if group is not None:
            ensure_period(session, group, period_days)
    return QuotaState(
        user_limit=user.quota_limit,
        user_used=user.pages_used,
        group_shared_quota=group.shared_quota if group else None,
        group_used=group.pages_used if group else 0,
        group_name=group.name if group else None,
    )


def authorize_job(
    session: Session,
    ctx: JobContext,
    *,
    cups_job_id: Optional[int] = None,
    settings: Optional[Settings] = None,
    charge_on_allow: bool = True,
) -> tuple[Decision, PrintJob]:
    """Evaluate a pending job and persist the resulting :class:`PrintJob`.

    When ``charge_on_allow`` is set (the default), the estimated pages are
    debited immediately so that two jobs submitted back to back cannot both
    pass a check against the same balance. The accounting daemon later
    adjusts the balance by the difference between estimate and actual.
    """
    settings = settings or get_settings()
    # The name CUPS reports may carry a domain prefix or different capitals
    # (CORP\J.Doe for j.doe); see services.identity.
    sent_as = ctx.username
    match = resolve_user(session, sent_as)
    user = match.user
    if user is None or not user.is_active:
        if match.ambiguous:
            reason = (
                f"'{sent_as}' matches more than one print account "
                f"({', '.join(match.candidates)}); rename or remove the duplicate"
            )
        elif user is None:
            reason = "no print account for this user"
        else:
            reason = "print account is disabled"
        job = PrintJob(
            cups_job_id=cups_job_id,
            username=user.username if user is not None else sent_as,
            printer=ctx.printer,
            title=ctx.title,
            copies=ctx.copies,
            is_color=ctx.is_color,
            is_duplex=ctx.is_duplex,
            estimated_pages=ctx.estimated_pages,
            status=PrintJob.STATUS_DENIED,
            denial_reason=reason,
        )
        # An unknown user has no FK target; record the denial without the row
        # so the backend can still report a reason.
        decision = Decision(allowed=False, reason=job.denial_reason, rule="unknown_user")
        log.info("pre-flight decision", extra={
            "user": sent_as, "printer": ctx.printer, "allowed": False, "reason": reason,
        })
        return decision, job

    if user.username != sent_as:
        # Charge, record and apply user-scoped policies to the real account.
        log.info("print user matched", extra={"sent_as": sent_as, "account": user.username, "how": match.how})
        ctx = dataclasses.replace(ctx, username=user.username)

    if user.group_name and not ctx.group_name:
        ctx = dataclasses.replace(ctx, group_name=user.group_name)

    state = load_quota_state(session, user, settings)
    rules = load_rules(session, ctx)
    decision = evaluate(
        rules,
        ctx,
        state,
        enforcement=str(settings.get("quota.enforcement", "strict")),
        enforce_group_budget=bool(settings.get("quota.enforce_group_budget", True)),
    )

    printer = session.get(Printer, ctx.printer)
    if printer is not None and not printer.is_active and decision.allowed:
        decision.deny(f"printer '{ctx.printer}' is disabled", "printer_inactive")

    is_duplex = ctx.is_duplex or "sides" in decision.forced_options
    rates = PrinterRates.from_printer(printer, settings.section("printing"))
    pages = max(int(ctx.estimated_pages), 0)

    job = PrintJob(
        cups_job_id=cups_job_id,
        username=ctx.username,
        printer=ctx.printer,
        title=ctx.title,
        copies=ctx.copies,
        is_color=ctx.is_color,
        is_duplex=is_duplex,
        estimated_pages=pages,
        status=PrintJob.STATUS_ALLOWED if decision.allowed else PrintJob.STATUS_DENIED,
        denial_reason=decision.reason,
        # A denied job costs nothing: it never reaches the paper. Recording a
        # notional cost here would inflate every chargeback report.
        cost=(
            job_cost(pages, rates, is_color=ctx.is_color, is_duplex=is_duplex)
            if decision.allowed
            else 0.0
        ),
    )

    if decision.allowed and charge_on_allow:
        _apply_charge(session, user, pages)
        job.charged_pages = pages
    session.add(job)
    session.flush()  # assign job.id so callers can reference the row
    log.info(
        "pre-flight decision",
        extra={
            "user": ctx.username,
            "printer": ctx.printer,
            "pages": pages,
            "allowed": decision.allowed,
            "rule": decision.rule,
            "reason": decision.reason,
        },
    )
    return decision, job


def _apply_charge(session: Session, user: User, pages: int) -> None:
    """Debit (or credit, when ``pages`` is negative) a user and their group."""
    if pages == 0:
        return
    user.pages_used = max(0, user.pages_used + pages)
    session.add(user)
    if user.group_name:
        group = session.get(Group, user.group_name)
        if group is not None:
            group.pages_used = max(0, group.pages_used + pages)
            session.add(group)


def charge_job(
    session: Session,
    job: PrintJob,
    actual_pages: int,
    *,
    settings: Optional[Settings] = None,
    completed_at: Optional[dt.datetime] = None,
) -> PrintJob:
    """Reconcile a completed job against the authoritative page count.

    Idempotent: a job already marked ``reconciled`` is returned untouched, so
    replaying ``page_log`` cannot double-charge.
    """
    settings = settings or get_settings()
    if job.reconciled:
        return job
    actual_pages = max(int(actual_pages), 0)
    delta = actual_pages - job.charged_pages
    user = session.get(User, job.username)
    if user is not None and delta:
        _apply_charge(session, user, delta)
    job.charged_pages = actual_pages
    job.actual_pages = actual_pages
    printer = session.get(Printer, job.printer)
    rates = PrinterRates.from_printer(printer, settings.section("printing"))
    job.cost = job_cost(actual_pages, rates, is_color=job.is_color, is_duplex=job.is_duplex)
    job.status = PrintJob.STATUS_COMPLETED
    job.completed_at = completed_at or utcnow()
    job.reconciled = True
    session.add(job)
    log.info(
        "job reconciled",
        extra={
            "user": job.username,
            "printer": job.printer,
            "estimated": job.estimated_pages,
            "actual": actual_pages,
            "delta": delta,
            "cost": job.cost,
        },
    )
    return job


def refund_job(session: Session, job: PrintJob, reason: str, admin_user: str = "system") -> PrintJob:
    """Credit back everything a job was charged (cancelled or failed job)."""
    if job.charged_pages:
        user = session.get(User, job.username)
        if user is not None:
            _apply_charge(session, user, -job.charged_pages)
    job.charged_pages = 0
    job.cost = 0.0
    job.status = PrintJob.STATUS_ERROR
    job.denial_reason = reason
    job.reconciled = True
    job.completed_at = utcnow()
    session.add(job)
    return job


def recent_denials(session: Session, username: str, window_hours: int = 1) -> int:
    """Count recent denials for a user (drives the repeated-denial alert)."""
    since = utcnow() - dt.timedelta(hours=window_hours)
    stmt = (
        select(PrintJob)
        .where(PrintJob.username == username)
        .where(PrintJob.status == PrintJob.STATUS_DENIED)
        .where(PrintJob.submitted_at >= since)
    )
    return len(session.scalars(stmt).all())


def usage_rows(
    session: Session,
    since: Optional[dt.datetime] = None,
    until: Optional[dt.datetime] = None,
    username: Optional[str] = None,
    printer: Optional[str] = None,
    statuses: Optional[Sequence[str]] = None,
) -> list[PrintJob]:
    """Filtered job list used by reports and CSV export."""
    stmt = select(PrintJob)
    if since:
        stmt = stmt.where(PrintJob.submitted_at >= since)
    if until:
        stmt = stmt.where(PrintJob.submitted_at <= until)
    if username:
        stmt = stmt.where(PrintJob.username == username)
    if printer:
        stmt = stmt.where(PrintJob.printer == printer)
    if statuses:
        stmt = stmt.where(PrintJob.status.in_(list(statuses)))
    stmt = stmt.order_by(PrintJob.submitted_at.desc())
    return list(session.scalars(stmt).all())
