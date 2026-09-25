"""Console user management beyond single edits: import, export, bulk actions.

Registered before :mod:`.admin` so ``/admin/users/import`` is not taken for
a username by ``/admin/users/{username}``.
"""

from __future__ import annotations

import datetime as dt
from urllib.parse import quote
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import Group, PrintPolicy, User
from ...services import user_import
from ...services.accounts import delete_user
from ...services.audit import record_audit
from ...services.quota import reset_user
from ..auth import get_db, require_admin
from ..deps import redirect, render

router = APIRouter(prefix="/admin")

MAX_UPLOAD_BYTES = 2 * 1024 * 1024

BULK_ACTIONS = {
    "set_group": "Move to group",
    "set_quota": "Set quota to",
    "reset": "Reset usage and restart period",
    "enable": "Enable",
    "disable": "Disable",
    "make_admin": "Grant admin rights",
    "revoke_admin": "Revoke admin rights",
    "delete": "Delete (with job history)",
}
#: Actions an administrator may not apply to their own account.
_NOT_ON_SELF = {"disable", "revoke_admin", "delete"}


# ----------------------------------------------------------------------- import
def _import_page(request: Request, admin: User, session: Session, **context):
    groups = session.scalars(select(Group.name).order_by(Group.name)).all()
    defaults = user_import.default_options()
    context.setdefault("text", "")
    context.setdefault("options", defaults)
    context.setdefault("plan", None)
    return render(
        request, "user_import.html", admin=admin, groups=groups, columns=user_import.COLUMNS, **context
    )


@router.get("/users/import", include_in_schema=False)
def import_form(
    request: Request, admin: User = Depends(require_admin), session: Session = Depends(get_db)
):
    """Upload or paste users to import."""
    return _import_page(request, admin, session)


@router.get("/users/import/template.csv", include_in_schema=False)
def import_template(admin: User = Depends(require_admin)):
    """A CSV template with the expected columns and two example rows."""
    return Response(
        user_import.TEMPLATE_CSV,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="printquota-users-template.csv"'},
    )


@router.post("/users/import", include_in_schema=False)
async def import_submit(
    request: Request,
    step: str = Form("preview"),
    text: str = Form(""),
    upload: Optional[UploadFile] = File(None),
    default_quota: str = Form(""),
    default_threshold: str = Form(""),
    default_group: str = Form(""),
    create_missing_groups: bool = Form(False),
    update_existing: bool = Form(False),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Preview an import (``step=preview``) or apply it (``step=apply``)."""
    if upload is not None and upload.filename:
        data = await upload.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            return _import_page(request, admin, session, error="That file is larger than 2 MB.")
        text = user_import.decode_upload(data)

    options = user_import.default_options(
        default_group=default_group or None,
        create_missing_groups=create_missing_groups,
        update_existing=update_existing,
    )
    for name, raw in (("default_quota", default_quota), ("default_threshold", default_threshold)):
        if raw.strip():
            if not raw.strip().isdigit():
                return _import_page(request, admin, session, text=text, options=options,
                                    error="Default quota and threshold must be whole numbers.")
            setattr(options, name, int(raw.strip()))

    if not text.strip():
        return _import_page(request, admin, session, options=options,
                            error="Choose a CSV file or paste at least one row.")
    try:
        if step == "apply":
            plan = user_import.apply_import(session, text, options, admin.username)
            message = (
                f"Import finished: {plan.count('create')} created, {plan.count('update')} updated, "
                f"{plan.count('skip')} skipped, {plan.count('error')} with errors."
            )
            if plan.groups_to_create:
                message += f" New groups: {', '.join(plan.groups_to_create)}."
            return redirect("/admin/users", message=message)
        plan = user_import.plan_import(session, text, options)
    except ValueError as exc:
        return _import_page(request, admin, session, text=text, options=options, error=str(exc))
    return _import_page(request, admin, session, text=text, options=options, plan=plan)


# ----------------------------------------------------------------------- export
@router.get("/users.csv", include_in_schema=False)
def users_export(admin: User = Depends(require_admin), session: Session = Depends(get_db)):
    """Every user in the import format (passwords are never exported)."""
    users = session.scalars(select(User).order_by(User.username)).all()
    record_audit(session, admin.username, "user.export", f"{len(users)} users", source="web")
    return Response(
        user_import.export_csv(list(users)),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="printquota-users-{dt.date.today()}.csv"'},
    )


# ------------------------------------------------------------------------- bulk
@router.post("/users/bulk", include_in_schema=False)
def users_bulk(
    usernames: list[str] = Form([]),
    action: str = Form(...),
    group_name: str = Form(""),
    quota_limit: str = Form(""),
    confirm: bool = Form(False),
    return_to: str = Form("/admin/users"),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Apply one action to every selected user."""
    back = return_to if return_to.startswith("/admin/users") else "/admin/users"
    if action not in BULK_ACTIONS:
        return redirect(back, error="Choose an action.")
    if not usernames:
        return redirect(back, error="Tick at least one user first.")
    if action == "delete" and not confirm:
        return redirect(back, error="Tick 'I understand' to confirm deleting users and their history.")

    target_group: Optional[str] = None
    if action == "set_group":
        target_group = group_name or None
        if target_group and session.get(Group, target_group) is None:
            return redirect(back, error=f"No such group: {target_group}")
    quota: Optional[int] = None
    if action == "set_quota":
        if not quota_limit.strip().isdigit():
            return redirect(back, error="Enter the new quota as a whole number of pages.")
        quota = int(quota_limit.strip())

    changed: list[str] = []
    skipped: list[str] = []
    for username in dict.fromkeys(usernames):
        user = session.get(User, username)
        if user is None:
            continue
        if username == admin.username and action in _NOT_ON_SELF:
            skipped.append(username)
            continue
        if action == "set_group":
            user.group_name = target_group
        elif action == "set_quota":
            user.quota_limit = quota
        elif action == "reset":
            reset_user(session, user)
        elif action == "enable":
            user.is_active = True
        elif action == "disable":
            user.is_active = False
        elif action == "make_admin":
            user.is_admin = True
        elif action == "revoke_admin":
            user.is_admin = False
        elif action == "delete":
            delete_user(session, user)
        changed.append(username)

    if changed:
        record_audit(
            session, admin.username, f"user.bulk.{action}", f"{len(changed)} users",
            {"users": changed, "group": target_group, "quota": quota}, source="web",
        )
    message = f"{BULK_ACTIONS[action]}: applied to {len(changed)} user(s)."
    if action == "set_group":
        message = f"Moved {len(changed)} user(s) to {target_group or 'no group'}."
    elif action == "set_quota":
        message = f"Set the quota of {len(changed)} user(s) to {quota} pages."
    if skipped:
        message += " Your own account was left unchanged."
    return redirect(back, message=message)


@router.post("/users/{username}/delete", include_in_schema=False)
def user_delete(
    username: str,
    confirm: bool = Form(False),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Delete one account, its job history and the policies that target it."""
    if username == admin.username:
        return redirect(f"/admin/users/{quote(username, safe='')}", error="You cannot delete your own account.")
    if not confirm:
        return redirect(f"/admin/users/{quote(username, safe='')}", error="Tick 'I understand' to confirm the deletion.")
    user = session.get(User, username)
    if user is None:
        return redirect("/admin/users", error=f"No such user: {username}")
    jobs = delete_user(session, user)
    record_audit(session, admin.username, "user.delete", username, {"jobs_deleted": jobs}, source="web")
    return redirect("/admin/users", message=f"Deleted {username}.")


# ----------------------------------------------------------------------- groups
@router.post("/groups/{name}/delete", include_in_schema=False)
def group_delete(
    name: str,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Delete a group: members become ungrouped, its policies are removed."""
    group = session.get(Group, name)
    if group is None:
        return redirect("/admin/groups", error=f"No such group: {name}")
    members = [u.username for u in group.users]
    for user in group.users:
        user.group_name = None
    removed = 0
    for policy in session.scalars(
        select(PrintPolicy).where(PrintPolicy.scope_type == "group", PrintPolicy.scope_value == name)
    ):
        session.delete(policy)
        removed += 1
    session.delete(group)
    record_audit(
        session, admin.username, "group.delete", name,
        {"members_ungrouped": members, "policies_removed": removed}, source="web",
    )
    return redirect(
        "/admin/groups",
        message=f"Deleted {name}. {len(members)} member(s) are now ungrouped; {removed} policy rule(s) removed.",
    )
