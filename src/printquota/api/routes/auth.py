"""Sign-in / sign-out routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ...core.config import get_settings
from ...core.logging import get_logger
from ..auth import authenticate, get_db, issue_session
from ..deps import render

router = APIRouter()
log = get_logger("api.auth")


@router.get("/login", include_in_schema=False)
def login_form(request: Request):
    """Render the sign-in page."""
    return render(request, "login.html", next_url=request.query_params.get("next", "/"))


@router.post("/login", include_in_schema=False)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_url: str = Form("/"),
    session: Session = Depends(get_db),
):
    """Validate credentials and set the session cookie."""
    user = authenticate(session, username.strip(), password)
    if user is None:
        log.warning("failed sign-in", extra={"user": username, "ip": request.client.host if request.client else "?"})
        response = render(
            request, "login.html", next_url=next_url, error="Incorrect username or password."
        )
        response.status_code = 401
        return response

    settings = get_settings()
    # Only follow relative redirect targets, never an absolute URL from the form.
    target = next_url if next_url.startswith("/") and not next_url.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        str(settings.get("api.session_cookie", "printquota_session")),
        issue_session(user.username),
        max_age=int(settings.get("api.session_max_age", 28800)),
        httponly=True,
        samesite="lax",
        secure=bool(settings.get("api.cookie_secure", False)),
        path="/",
    )
    log.info("sign-in", extra={"user": user.username, "admin": user.is_admin})
    return response


@router.get("/logout", include_in_schema=False)
def logout():
    """Clear the session cookie."""
    settings = get_settings()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(str(settings.get("api.session_cookie", "printquota_session")), path="/")
    return response
