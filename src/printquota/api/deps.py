"""Shared web-layer helpers (templates, flash messages, formatting)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Optional

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..core.config import get_settings

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _fmt_dt(value: Optional[dt.datetime], fmt: str = "%Y-%m-%d %H:%M") -> str:
    return value.strftime(fmt) if value else "-"


templates.env.filters["dt"] = _fmt_dt


def render(request: Request, template: str, **context):
    """Render a template with the common context injected."""
    settings = get_settings()
    context.setdefault("currency", settings.get("printing.currency", ""))
    context.setdefault("period_days", int(settings.get("quota.period_days", 30)))
    context.setdefault("flash", request.query_params.get("msg"))
    context.setdefault("error", request.query_params.get("err"))
    return templates.TemplateResponse(request, template, context)


def redirect(url: str, message: Optional[str] = None, error: Optional[str] = None) -> RedirectResponse:
    """303-redirect after a POST, carrying a one-shot message."""
    from urllib.parse import quote

    if message:
        url += ("&" if "?" in url else "?") + "msg=" + quote(message)
    if error:
        url += ("&" if "?" in url else "?") + "err=" + quote(error)
    return RedirectResponse(url, status_code=303)
