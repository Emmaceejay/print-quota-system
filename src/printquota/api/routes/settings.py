"""Console settings: quotas, costs and alert delivery, edited in the browser."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ...core.config import env_overridden_keys
from ...db.models import User
from ...notifications.alerts import send_alert
from ...services import runtime_settings
from ...services.audit import record_audit
from ..auth import get_db, require_admin
from ..deps import redirect, render

router = APIRouter(prefix="/admin")


@router.get("/settings", include_in_schema=False)
def settings_page(
    request: Request, admin: User = Depends(require_admin), session: Session = Depends(get_db)
):
    """Every console-editable setting with its current value and where it comes from."""
    states = runtime_settings.describe(session)
    by_section = {
        section: [s for s in states if s.spec.section == section]
        for section in runtime_settings.SECTIONS
    }
    return render(
        request,
        "settings.html",
        admin=admin,
        sections=by_section,
        summary=runtime_settings.config_summary(),
    )


@router.post("/settings", include_in_schema=False)
async def settings_save(
    request: Request, admin: User = Depends(require_admin), session: Session = Depends(get_db)
):
    """Validate and save the whole form (nothing is written if any field is invalid)."""
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
    try:
        changed = runtime_settings.save(session, submitted, admin.username)
    except ValueError as exc:
        return redirect("/admin/settings", error=f"Nothing was saved. {exc}")
    if not changed:
        return redirect("/admin/settings", message="No changes to save.")
    labels = [runtime_settings.FIELDS_BY_KEY[key].label for key in changed]
    return redirect("/admin/settings", message=f"Saved: {', '.join(labels)}.")


@router.post("/settings/revert", include_in_schema=False)
def settings_revert(
    key: str = Form(...), admin: User = Depends(require_admin), session: Session = Depends(get_db)
):
    """Drop a console value so the configuration file (or default) applies again."""
    spec = runtime_settings.FIELDS_BY_KEY.get(key)
    if spec is None:
        return redirect("/admin/settings", error="Unknown setting.")
    if runtime_settings.revert(session, key, admin.username):
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
