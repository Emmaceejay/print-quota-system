"""Admin console: users, groups, printers, policies, reports, audit."""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...core.config import get_settings
from ...db.models import (
    AdminAuditLog,
    AlertLog,
    Group,
    PrintJob,
    PrintPolicy,
    Printer,
    User,
    utcnow,
)
from ...policies.engine import validate_rule
from ...services.audit import record_audit
from ...services.quota import reset_user, usage_rows
from ..auth import get_db, hash_password, require_admin
from ..deps import redirect, render

router = APIRouter(prefix="/admin")


def _totals(session: Session, since: dt.datetime) -> dict:
    jobs = session.scalars(
        select(PrintJob).where(PrintJob.submitted_at >= since)
    ).all()
    return {
        "jobs": len(jobs),
        "denied": sum(1 for j in jobs if j.status == PrintJob.STATUS_DENIED),
        "pages": sum(j.charged_pages for j in jobs),
        # Denied jobs carry a zero cost, so this is the same figure the
        # reports page and the CSV export show.
        "cost": sum(j.cost or 0 for j in jobs),
    }


@router.get("", include_in_schema=False)
def dashboard(
    request: Request,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Overview: 30-day totals, users near their limit, recent denials."""
    since = utcnow() - dt.timedelta(days=30)
    totals = _totals(session, since)
    users = session.scalars(select(User).where(User.is_active.is_(True))).all()
    low = sorted(
        (u for u in users if u.remaining <= u.low_balance_threshold),
        key=lambda u: u.remaining,
    )[:10]
    denied = session.scalars(
        select(PrintJob)
        .where(PrintJob.status == PrintJob.STATUS_DENIED)
        .order_by(PrintJob.submitted_at.desc())
        .limit(10)
    ).all()
    top = session.execute(
        select(PrintJob.username, func.sum(PrintJob.charged_pages).label("pages"))
        .where(PrintJob.submitted_at >= since)
        .group_by(PrintJob.username)
        .order_by(func.sum(PrintJob.charged_pages).desc())
        .limit(10)
    ).all()
    return render(
        request,
        "dashboard.html",
        admin=admin,
        totals=totals,
        low_users=low,
        denied=denied,
        top=top,
        user_count=len(users),
        printer_count=session.scalar(select(func.count()).select_from(Printer)),
    )


# ------------------------------------------------------------------------ users
@router.get("/users", include_in_schema=False)
def users_page(
    request: Request,
    q: str = "",
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Searchable user list with inline quota editing."""
    stmt = select(User).order_by(User.username)
    if q:
        like = f"%{q}%"
        stmt = stmt.where((User.username.like(like)) | (User.display_name.like(like)))
    users = session.scalars(stmt).all()
    groups = session.scalars(select(Group).order_by(Group.name)).all()
    return render(request, "users.html", admin=admin, users=users, groups=groups, q=q)


@router.post("/users/create", include_in_schema=False)
def users_create(
    username: str = Form(...),
    display_name: str = Form(""),
    email: str = Form(""),
    group_name: str = Form(""),
    quota_limit: int = Form(...),
    password: str = Form(""),
    is_admin: bool = Form(False),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Create a print account from the console."""
    username = username.strip()
    if not username:
        return redirect("/admin/users", error="Username is required.")
    if session.get(User, username) is not None:
        return redirect("/admin/users", error=f"User '{username}' already exists.")
    settings = get_settings()
    session.add(
        User(
            username=username,
            display_name=display_name.strip() or username,
            email=email.strip() or None,
            group_name=group_name or None,
            quota_limit=max(int(quota_limit), 0),
            low_balance_threshold=int(settings.get("quota.default_low_balance_threshold", 50)),
            is_admin=bool(is_admin),
            password_hash=hash_password(password) if password else None,
        )
    )
    record_audit(session, admin.username, "user.add", username, {"quota": quota_limit}, source="web")
    return redirect("/admin/users", message=f"Created {username}.")


@router.get("/users/{username}", include_in_schema=False)
def user_detail(
    request: Request,
    username: str,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """One user: balance, policies that target them, recent jobs."""
    user = session.get(User, username)
    if user is None:
        response = render(request, "error.html", code=404, message=f"No such user: {username}")
        response.status_code = 404
        return response
    jobs = usage_rows(session, username=username)[:50]
    policies = session.scalars(
        select(PrintPolicy).where(
            (PrintPolicy.scope_type == "user") & (PrintPolicy.scope_value == username)
        )
    ).all()
    alerts = session.scalars(
        select(AlertLog).where(AlertLog.username == username).order_by(AlertLog.sent_at.desc()).limit(10)
    ).all()
    groups = session.scalars(select(Group).order_by(Group.name)).all()
    period_days = int(get_settings().get("quota.period_days", 30))
    return render(
        request,
        "user_detail.html",
        admin=admin,
        user=user,
        jobs=jobs,
        policies=policies,
        alerts=alerts,
        groups=groups,
        period_end=user.period_end(period_days),
    )


@router.post("/users/{username}/update", include_in_schema=False)
def user_update(
    username: str,
    quota_limit: int = Form(...),
    low_balance_threshold: int = Form(...),
    email: str = Form(""),
    group_name: str = Form(""),
    is_active: bool = Form(False),
    is_admin: bool = Form(False),
    password: str = Form(""),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Update a user's quota, group and flags."""
    user = session.get(User, username)
    if user is None:
        return redirect("/admin/users", error=f"No such user: {username}")
    if user.username == admin.username and not is_admin:
        return redirect(f"/admin/users/{username}", error="You cannot remove your own admin rights.")
    before = {"quota": user.quota_limit, "group": user.group_name, "active": user.is_active}
    user.quota_limit = max(int(quota_limit), 0)
    user.low_balance_threshold = max(int(low_balance_threshold), 0)
    user.email = email.strip() or None
    user.group_name = group_name or None
    user.is_active = bool(is_active)
    user.is_admin = bool(is_admin)
    if password:
        user.password_hash = hash_password(password)
    record_audit(
        session,
        admin.username,
        "user.update",
        username,
        {"before": before, "after": {"quota": user.quota_limit, "group": user.group_name}},
        source="web",
    )
    return redirect(f"/admin/users/{username}", message="Saved.")


@router.post("/users/{username}/reset", include_in_schema=False)
def user_reset_usage(
    username: str,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Zero a user's usage and restart their rolling period."""
    user = session.get(User, username)
    if user is None:
        return redirect("/admin/users", error=f"No such user: {username}")
    before = user.pages_used
    reset_user(session, user)
    record_audit(session, admin.username, "user.reset", username, {"was": before}, source="web")
    return redirect(f"/admin/users/{username}", message=f"Usage reset (was {before} pages).")


# ----------------------------------------------------------------------- groups
@router.get("/groups", include_in_schema=False)
def groups_page(
    request: Request,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Groups and their shared budgets."""
    groups = session.scalars(select(Group).order_by(Group.name)).all()
    return render(request, "groups.html", admin=admin, groups=groups)


@router.post("/groups/create", include_in_schema=False)
def groups_create(
    name: str = Form(...),
    description: str = Form(""),
    shared_quota: str = Form(""),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Create a group, optionally with a shared page pool."""
    name = name.strip()
    if not name:
        return redirect("/admin/groups", error="Group name is required.")
    if session.get(Group, name) is not None:
        return redirect("/admin/groups", error=f"Group '{name}' already exists.")
    budget = int(shared_quota) if shared_quota.strip().isdigit() else None
    session.add(Group(name=name, description=description.strip() or None, shared_quota=budget))
    record_audit(session, admin.username, "group.add", name, {"budget": budget}, source="web")
    return redirect("/admin/groups", message=f"Created {name}.")


@router.post("/groups/{name}/update", include_in_schema=False)
def groups_update(
    name: str,
    shared_quota: str = Form(""),
    description: str = Form(""),
    reset: bool = Form(False),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Change a group's budget, or reset its pool usage."""
    group = session.get(Group, name)
    if group is None:
        return redirect("/admin/groups", error=f"No such group: {name}")
    group.shared_quota = int(shared_quota) if shared_quota.strip().isdigit() else None
    group.description = description.strip() or None
    if reset:
        group.pages_used = 0
        group.period_start = utcnow()
    record_audit(
        session, admin.username, "group.update", name, {"budget": group.shared_quota, "reset": reset}, source="web"
    )
    return redirect("/admin/groups", message=f"Saved {name}.")


# --------------------------------------------------------------------- printers
@router.get("/printers", include_in_schema=False)
def printers_page(
    request: Request,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Printers and their cost model."""
    printers = session.scalars(select(Printer).order_by(Printer.name)).all()
    return render(request, "printers.html", admin=admin, printers=printers)


@router.post("/printers/save", include_in_schema=False)
def printers_save(
    name: str = Form(...),
    real_device_uri: str = Form(""),
    cost_per_page_mono: float = Form(0.0),
    cost_per_page_color: float = Form(0.0),
    duplex_discount: float = Form(0.0),
    supports_duplex: bool = Form(False),
    is_active: bool = Form(True),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Create or update a printer row (upsert by queue name)."""
    name = name.strip()
    if not name:
        return redirect("/admin/printers", error="Printer name is required.")
    if not 0 <= duplex_discount < 1:
        return redirect("/admin/printers", error="Duplex discount must be >= 0 and < 1.")
    printer = session.get(Printer, name)
    created = printer is None
    if printer is None:
        printer = Printer(name=name)
        session.add(printer)
    printer.real_device_uri = real_device_uri.strip() or None
    printer.cost_per_page_mono = max(float(cost_per_page_mono), 0.0)
    printer.cost_per_page_color = max(float(cost_per_page_color), 0.0)
    printer.duplex_discount = float(duplex_discount)
    printer.supports_duplex = bool(supports_duplex)
    printer.is_active = bool(is_active)
    record_audit(
        session, admin.username, "printer.add" if created else "printer.update", name, source="web"
    )
    return redirect("/admin/printers", message=f"Saved {name}.")


# --------------------------------------------------------------------- policies
@router.get("/policies", include_in_schema=False)
def policies_page(
    request: Request,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Policy rules, with the pickers needed to add one."""
    policies = session.scalars(select(PrintPolicy).order_by(PrintPolicy.id)).all()
    return render(
        request,
        "policies.html",
        admin=admin,
        policies=policies,
        scopes=PrintPolicy.SCOPES,
        rules=PrintPolicy.RULES,
        users=session.scalars(select(User.username).order_by(User.username)).all(),
        groups=session.scalars(select(Group.name).order_by(Group.name)).all(),
        printers=session.scalars(select(Printer.name).order_by(Printer.name)).all(),
    )


@router.post("/policies/create", include_in_schema=False)
def policies_create(
    scope_type: str = Form(...),
    scope_value: str = Form(""),
    rule_type: str = Form(...),
    rule_value: str = Form("true"),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Add a policy rule after validating it."""
    if scope_type not in PrintPolicy.SCOPES:
        return redirect("/admin/policies", error="Unknown scope.")
    if scope_type != "global" and not scope_value.strip():
        return redirect("/admin/policies", error=f"A {scope_type} must be selected.")
    try:
        validate_rule(rule_type, rule_value or "true")
    except ValueError as exc:
        return redirect("/admin/policies", error=str(exc))
    policy = PrintPolicy(
        scope_type=scope_type,
        scope_value=None if scope_type == "global" else scope_value.strip(),
        rule_type=rule_type,
        rule_value=(rule_value or "true").strip(),
    )
    session.add(policy)
    record_audit(
        session,
        admin.username,
        "policy.add",
        f"{scope_type}:{scope_value}",
        {"rule": rule_type, "value": rule_value},
        source="web",
    )
    return redirect("/admin/policies", message="Policy added.")


@router.post("/policies/{policy_id}/delete", include_in_schema=False)
def policies_delete(
    policy_id: int,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Remove a policy rule."""
    policy = session.get(PrintPolicy, policy_id)
    if policy is None:
        return redirect("/admin/policies", error="No such policy.")
    session.delete(policy)
    record_audit(session, admin.username, "policy.remove", str(policy_id), source="web")
    return redirect("/admin/policies", message="Policy removed.")


# ---------------------------------------------------------------------- reports
def _report_filters(days: int, username: str, printer: str, status: str) -> dict:
    return {
        "since": utcnow() - dt.timedelta(days=max(days, 1)),
        "username": username or None,
        "printer": printer or None,
        "statuses": [status] if status else None,
    }


@router.get("/reports", include_in_schema=False)
def reports_page(
    request: Request,
    days: int = 30,
    username: str = "",
    printer: str = "",
    status: str = "",
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Usage report with filters, totals and a CSV export link."""
    filters = _report_filters(days, username, printer, status)
    jobs = usage_rows(session, **filters)[:500]
    totals = {
        "jobs": len(jobs),
        "pages": sum(j.charged_pages for j in jobs),
        "cost": sum(j.cost or 0 for j in jobs),
        "denied": sum(1 for j in jobs if j.status == PrintJob.STATUS_DENIED),
    }
    return render(
        request,
        "reports.html",
        admin=admin,
        jobs=jobs,
        totals=totals,
        days=days,
        username=username,
        printer=printer,
        status=status,
        printers=session.scalars(select(Printer.name).order_by(Printer.name)).all(),
    )


@router.get("/reports.csv", include_in_schema=False)
def reports_csv(
    days: int = 30,
    username: str = "",
    printer: str = "",
    status: str = "",
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Export the filtered report as CSV."""
    jobs = usage_rows(session, **_report_filters(days, username, printer, status))
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "submitted_at",
            "completed_at",
            "username",
            "printer",
            "title",
            "copies",
            "color",
            "duplex",
            "estimated_pages",
            "actual_pages",
            "charged_pages",
            "cost",
            "status",
            "denial_reason",
        ]
    )
    for job in jobs:
        writer.writerow(
            [
                job.submitted_at.isoformat(),
                job.completed_at.isoformat() if job.completed_at else "",
                job.username,
                job.printer,
                job.title or "",
                job.copies,
                int(job.is_color),
                int(job.is_duplex),
                job.estimated_pages if job.estimated_pages is not None else "",
                job.actual_pages if job.actual_pages is not None else "",
                job.charged_pages,
                f"{job.cost or 0:.4f}",
                job.status,
                job.denial_reason or "",
            ]
        )
    record_audit(session, admin.username, "report.export", f"{len(jobs)} rows", source="web")
    buffer.seek(0)
    filename = f"printquota-usage-{dt.date.today()}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/audit", include_in_schema=False)
def audit_page(
    request: Request,
    limit: int = 100,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Recent administrative actions."""
    entries = session.scalars(
        select(AdminAuditLog).order_by(AdminAuditLog.timestamp.desc()).limit(max(1, min(limit, 500)))
    ).all()
    return render(request, "audit.html", admin=admin, entries=entries)


@router.get("/api/summary", tags=["admin"])
def api_summary(
    days: int = 30,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
) -> dict:
    """JSON summary for dashboards and monitoring."""
    since = utcnow() - dt.timedelta(days=max(days, 1))
    totals = _totals(session, since)
    return {
        "window_days": days,
        "jobs": totals["jobs"],
        "denied": totals["denied"],
        "pages": totals["pages"],
        "cost": round(totals["cost"], 2),
        "users": session.scalar(select(func.count()).select_from(User)),
        "printers": session.scalar(select(func.count()).select_from(Printer)),
    }
