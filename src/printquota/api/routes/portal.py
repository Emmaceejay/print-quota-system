"""Self-service portal: a user's own balance and history."""

from __future__ import annotations

import csv
import datetime as dt
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from ...core.config import get_settings
from ...db.models import User, utcnow
from ...services.quota import load_quota_state, usage_rows
from ..auth import current_user, get_db
from ..deps import render

router = APIRouter()


@router.get("/", include_in_schema=False)
def index(user: User = Depends(current_user)):
    """Send admins to the console and everyone else to their own page."""
    return RedirectResponse("/admin" if user.is_admin else "/me", status_code=303)


@router.get("/me", include_in_schema=False)
def me(
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
):
    """The signed-in user's balance, period and recent jobs."""
    settings = get_settings()
    state = load_quota_state(session, user, settings)
    jobs = usage_rows(session, username=user.username)[:50]
    period_days = int(settings.get("quota.period_days", 30))
    used_pct = 0 if user.quota_limit == 0 else min(100, round(user.pages_used * 100 / user.quota_limit))
    return render(
        request,
        "portal.html",
        user=user,
        state=state,
        jobs=jobs,
        used_pct=used_pct,
        period_end=user.period_end(period_days),
    )


@router.get("/me/history.csv", include_in_schema=False)
def my_history_csv(
    days: int = 90,
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
):
    """Download the signed-in user's own job history."""
    since = utcnow() - dt.timedelta(days=days)
    jobs = usage_rows(session, since=since, username=user.username)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["submitted_at", "printer", "title", "pages", "cost", "status", "reason"])
    for job in jobs:
        writer.writerow(
            [
                job.submitted_at.isoformat(),
                job.printer,
                job.title or "",
                job.charged_pages,
                f"{job.cost or 0:.4f}",
                job.status,
                job.denial_reason or "",
            ]
        )
    buffer.seek(0)
    filename = f"printquota-{user.username}-{dt.date.today()}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/me", tags=["portal"])
def api_me(user: User = Depends(current_user), session: Session = Depends(get_db)) -> dict:
    """JSON balance for the signed-in user (used by client agents)."""
    state = load_quota_state(session, user, get_settings())
    return {
        "username": user.username,
        "quota_limit": user.quota_limit,
        "pages_used": user.pages_used,
        "remaining": user.remaining,
        "group": state.group_name,
        "group_remaining": state.group_remaining,
        "period_start": user.period_start.isoformat(),
    }
