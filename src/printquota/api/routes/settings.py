"""Console settings: quotas, costs and alert delivery, edited in the browser."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ...core.config import env_overridden_keys, invalidate_settings
from ...db.models import User
from ...notifications.alerts import send_alert
from ...services import runtime_settings
from ...services.audit import record_audit
from ..auth import get_db, require_admin
from ..deps import redirect, render

router = APIRouter(prefix="/admin")


def _render_settings(request: Request, admin: User, session: Session, **context):
    states = runtime_settings.describe(session)
    by_section = {
        section: [s for s in states if s.spec.section == section]
        for section in runtime_settings.SECTIONS
    }
    context.setdefault("field_errors", {})
    context.setdefault("submitted", {})
    return render(
        request,
        "settings.html",
        admin=admin,
        sections=by_section,
        summary=runtime_settings.config_summary(),
        **context,
    )


@router.get("/settings", include_in_schema=False)
def settings_page(
    request: Request, admin: User = Depends(require_admin), session: Session = Depends(get_db)
):
    """Every console-editable setting with its current value and where it comes from."""
    return _render_settings(request, admin, session)


@router.post("/settings", include_in_schema=False)
async def settings_save(
    request: Request, admin: User = Depends(require_admin), session: Session = Depends(get_db)
):
    """Save every valid change; show any field that could not be saved in place."""
    form = await request.form()
    pinned = env_overridden_keys()
    submitted: dict[str, str] = {}
    for spec in runtime_settings.FIELDS:
        if spec.key in pinned:
            continue
        if spec.kind == "bool":
            # Unticked checkboxes are simply absent from the form.
            submitted[spec.key] = "true" if form.get(spec.key) else "false"
        elif spec.key in form:
            submitted[spec.key] = str(form.get(spec.key))

    result = runtime_settings.save(session, submitted, admin.username)
    # Commit before anything re-reads settings, or the old values would be
    # cached (for up to 15 s) from a read that cannot see this transaction.
    session.commit()
    invalidate_settings()
    labels = [runtime_settings.FIELDS_BY_KEY[key].label for key in result.changed]

    if not result.errors:
        if not labels:
            return redirect("/admin/settings", message="No changes to save.")
        return redirect("/admin/settings", message=f"Saved: {', '.join(labels)}.")

    if len(result.errors) == 1:
        problem = "1 setting could not be saved. It is marked in red below: correct or clear it and save again."
    else:
        problem = (f"{len(result.errors)} settings could not be saved. They are marked in red below: "
                   "correct or clear them and save again.")
    response = _render_settings(
        request, admin, session,
        flash=f"Saved: {', '.join(labels)}." if labels else None,
        error=problem,
        field_errors=result.errors,
        submitted={key: submitted.get(key, "") for key in result.errors},
    )
    response.status_code = 400
    return response


@router.post("/settings/revert", include_in_schema=False)
def settings_revert(
    key: str = Form(...), admin: User = Depends(require_admin), session: Session = Depends(get_db)
):
    """Drop a console value so the configuration file (or default) applies again."""
    spec = runtime_settings.FIELDS_BY_KEY.get(key)
    if spec is None:
        return redirect("/admin/settings", error="Unknown setting.")
    reverted = runtime_settings.revert(session, key, admin.username)
    session.commit()
    invalidate_settings()
    if reverted:
        return redirect("/admin/settings", message=f"{spec.label} now uses the configuration file value.")
    return redirect("/admin/settings", message=f"{spec.label} was not set from the console.")


@router.post("/settings/test-alert", include_in_schema=False)
def settings_test_alert(admin: User = Depends(require_admin), session: Session = Depends(get_db)):
    """Send a test notification to the signed-in administrator."""
    entry = send_alert(
        session,
        admin,
        "test",
        "printquota test alert",
        f"Hello {admin.display_name or admin.username},\n\n"
        "This is a test message from printquota. If you can read it, alert delivery works.\n",
        force=True,
    )
    record_audit(session, admin.username, "alerts.test", admin.username,
                 {"delivered": bool(entry and entry.delivered)}, source="web")
    if entry is None:
        return redirect("/admin/settings", error="Alerts are switched off (or the channel is 'none').")
    if entry.delivered:
        target = admin.email if entry.channel == "email" else "the webhook URL"
        return redirect("/admin/settings", message=f"Test alert delivered to {target}.")
    if entry.channel == "email" and not admin.email:
        return redirect("/admin/settings", error="Add an email address to your own account first, then try again.")
    return redirect(
        "/admin/settings",
        error="The test alert could not be delivered. Check the server/URL and credentials; "
              "details are in the quota-api journal.",
    )
