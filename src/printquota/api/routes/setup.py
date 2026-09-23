"""First-run setup: create the first administrator from the browser.

The wizard is open only while no active administrator exists. Because the
first person to reach it becomes an administrator, it is guarded by the
one-time ``setup_token`` that install.sh generates and prints. When no token
is configured (for example a development checkout), it only accepts
requests from the server itself.
"""

from __future__ import annotations

import re
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.config import get_settings
from ...core.logging import get_logger
from ...db.models import User
from ...services.audit import record_audit
from ..auth import get_db, hash_password, issue_session
from ..deps import render

router = APIRouter()
log = get_logger("api.setup")

USERNAME_RE = re.compile(r"^[A-Za-z0-9._@\\\-]{1,128}$")
MIN_PASSWORD_LENGTH = 8
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def needs_setup(session: Session) -> bool:
    """True while there is no active administrator."""
    stmt = select(User.username).where(User.is_admin.is_(True)).where(User.is_active.is_(True)).limit(1)
    return session.scalar(stmt) is None


def _token_ok(request: Request, supplied: Optional[str]) -> bool:
    expected = str(get_settings().get("setup_token") or "").strip()
    if expected:
        return bool(supplied) and secrets.compare_digest(supplied.strip(), expected)
    host = request.client.host if request.client else ""
    return host in _LOOPBACK


def _closed(request: Request):
    response = render(
        request, "error.html", code=404,
        message="Setup is already complete. Sign in with an administrator account.",
    )
    response.status_code = 404
    return response


@router.get("/setup", include_in_schema=False)
def setup_form(request: Request, token: str = "", session: Session = Depends(get_db)):
    """Show the create-administrator form."""
    if not needs_setup(session):
        return _closed(request)
    token_required = bool(str(get_settings().get("setup_token") or "").strip())
    return render(
        request,
        "setup.html",
        token=token,
        token_required=token_required,
        token_valid=_token_ok(request, token) if token else False,
        form={},
        min_length=MIN_PASSWORD_LENGTH,
    )


@router.post("/setup", include_in_schema=False)
def setup_submit(
    request: Request,
    token: str = Form(""),
    username: str = Form(...),
    display_name: str = Form(""),
    email: str = Form(""),
    password: str = Form(...),
    password_confirm: str = Form(...),
    session: Session = Depends(get_db),
):
    """Create (or promote) the first administrator and sign them in."""
    if not needs_setup(session):
        return _closed(request)
    token_required = bool(str(get_settings().get("setup_token") or "").strip())
    form = {"username": username, "display_name": display_name, "email": email}

    def fail(message: str, status: int = 400):
        response = render(
            request, "setup.html", token=token, token_required=token_required,
            token_valid=False, form=form, error=message, min_length=MIN_PASSWORD_LENGTH,
        )
        response.status_code = status
        return response

    if not _token_ok(request, token):
        client = request.client.host if request.client else "?"
        log.warning("setup attempt with a bad token", extra={"ip": client})
        if token_required:
            return fail("The setup token is not correct. Copy it from the installer's output "
                        "(it is also PRINTQUOTA_SETUP_TOKEN in /etc/printquota/env).", 403)
        return fail("Without a setup token, setup can only be completed from the server itself.", 403)

    username = username.strip()
    if not USERNAME_RE.fullmatch(username):
        return fail("Usernames may use letters, digits and . _ @ \\ - (no spaces).")
    if len(password) < MIN_PASSWORD_LENGTH:
        return fail(f"Use a password of at least {MIN_PASSWORD_LENGTH} characters.")
    if password != password_confirm:
        return fail("The two passwords do not match.")
    email = email.strip()
    if email and "@" not in email:
        return fail("Enter a valid email address or leave it blank.")

    settings = get_settings()
    user = session.get(User, username)
    created = user is None
    if user is None:
        user = User(
            username=username,
            quota_limit=int(settings.get("quota.default_limit", 500)),
            low_balance_threshold=int(settings.get("quota.default_low_balance_threshold", 50)),
        )
        session.add(user)
    user.display_name = display_name.strip() or user.display_name or username
    user.email = email or user.email
    user.is_admin = True
    user.is_active = True
    user.password_hash = hash_password(password)
    record_audit(session, username, "setup.admin", username, {"created": created}, source="web")
    log.info("first administrator created", extra={"user": username})

    response = RedirectResponse("/admin?msg=Welcome!+Your+administrator+account+is+ready.", status_code=303)
    response.set_cookie(
        str(settings.get("api.session_cookie", "printquota_session")),
        issue_session(username),
        max_age=int(settings.get("api.session_max_age", 28800)),
        httponly=True,
        samesite="lax",
        secure=bool(settings.get("api.cookie_secure", False)),
        path="/",
    )
    return response
