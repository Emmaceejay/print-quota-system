"""``quotactl`` -- the administrative CLI.

Everything the web console can do is scriptable here, which is what makes
bootstrapping, cron-style automation and disaster recovery possible without
a browser. Every mutating command writes an ``admin_audit_log`` row.
"""

from __future__ import annotations

import csv
import datetime as dt
import getpass
import os
import sys
from typing import Optional

import click
from sqlalchemy import func, select

from ..core.config import get_settings, reset_settings_cache
from ..db import session as db_session
from ..db.models import (
    AdminAuditLog,
    AlertLog,
    Group,
    PrintJob,
    PrintPolicy,
    Printer,
    User,
    utcnow,
)
from ..policies.engine import validate_rule
from ..services import cups_queues
from ..services.accounts import delete_user
from ..services.identity import new_account_problem
from ..services.audit import record_audit
from ..services.quota import reset_expired_periods, reset_user, usage_rows


def _actor() -> str:
    return os.environ.get("SUDO_USER") or getpass.getuser()


def _echo_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        click.echo("(none)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    click.echo(click.style(fmt.format(*headers), bold=True))
    for row in rows:
        click.echo(fmt.format(*[str(c) for c in row]))


def _hash_password(password: str) -> str:
    from ..api.auth import hash_password

    return hash_password(password)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--config", "config_path", type=click.Path(exists=True), help="settings.yaml path")
@click.option("--db-url", default=None, help="Override the database URL.")
@click.version_option(package_name="printquota", prog_name="quotactl")
def cli(config_path: Optional[str], db_url: Optional[str]) -> None:
    """Administer the print quota system."""
    if config_path:
        os.environ["PRINTQUOTA_CONFIG"] = config_path
    if db_url:
        os.environ["PRINTQUOTA_DB_URL"] = db_url
    reset_settings_cache()
    db_session.reset()


# --------------------------------------------------------------------------- db
@cli.group()
def db() -> None:
    """Datastore maintenance."""


@db.command("init")
@click.option("--admin", default=None, help="Create an initial admin user with this username.")
@click.option("--admin-password", default=None, help="Password for the initial admin user.")
def db_init(admin: Optional[str], admin_password: Optional[str]) -> None:
    """Create the schema (equivalent to `alembic upgrade head` on an empty DB)."""
    db_session.create_all()
    click.echo(f"schema ready at {get_settings().get('database.url')}")
    if admin:
        password = admin_password or getpass.getpass(f"password for {admin}: ")
        settings = get_settings()
        with db_session.session_scope() as session:
            user = session.get(User, admin)
            if user is None:
                user = User(
                    username=admin,
                    display_name=admin,
                    quota_limit=int(settings.get("quota.default_limit", 500)),
                    low_balance_threshold=int(
                        settings.get("quota.default_low_balance_threshold", 50)
                    ),
                )
                session.add(user)
            user.is_admin = True
            user.password_hash = _hash_password(password)
            record_audit(session, _actor(), "db.init", admin, {"admin_created": True})
        click.echo(f"admin user '{admin}' ready")


@db.command("stats")
def db_stats() -> None:
    """Row counts and current period totals."""
    with db_session.session_scope() as session:
        rows = [
            ["users", session.scalar(select(func.count()).select_from(User))],
            ["groups", session.scalar(select(func.count()).select_from(Group))],
            ["printers", session.scalar(select(func.count()).select_from(Printer))],
            ["policies", session.scalar(select(func.count()).select_from(PrintPolicy))],
            ["jobs", session.scalar(select(func.count()).select_from(PrintJob))],
            ["alerts", session.scalar(select(func.count()).select_from(AlertLog))],
        ]
    _echo_table(["table", "rows"], [[a, b] for a, b in rows])


# ------------------------------------------------------------------------- user
@cli.group()
def user() -> None:
    """Manage print users."""


@user.command("add")
@click.argument("username")
@click.option("--display-name", default=None)
@click.option("--email", default=None)
@click.option("--group", "group_name", default=None)
@click.option("--quota", type=int, default=None, help="Page allowance per period.")
@click.option("--threshold", type=int, default=None, help="Low-balance alert threshold.")
@click.option("--admin", is_flag=True, help="Grant web console admin rights.")
@click.option("--password", default=None, help="Set a web portal password.")
def user_add(
    username: str,
    display_name: Optional[str],
    email: Optional[str],
    group_name: Optional[str],
    quota: Optional[int],
    threshold: Optional[int],
    admin: bool,
    password: Optional[str],
) -> None:
    """Create a print account."""
    settings = get_settings()
    with db_session.session_scope() as session:
        problem = new_account_problem(session, username)
        if problem:
            raise click.ClickException(problem)
        if group_name and session.get(Group, group_name) is None:
            raise click.ClickException(f"group '{group_name}' does not exist")
        record = User(
            username=username,
            display_name=display_name or username,
            email=email,
            group_name=group_name,
            quota_limit=quota if quota is not None else int(settings.get("quota.default_limit", 500)),
            low_balance_threshold=(
                threshold
                if threshold is not None
                else int(settings.get("quota.default_low_balance_threshold", 50))
            ),
            is_admin=admin,
            password_hash=_hash_password(password) if password else None,
        )
        session.add(record)
        record_audit(
            session,
            _actor(),
            "user.add",
            username,
            {"quota": record.quota_limit, "group": group_name, "admin": admin},
        )
    click.echo(f"created user '{username}'")


@user.command("list")
@click.option("--group", "group_name", default=None)
@click.option("--inactive", is_flag=True, help="Include disabled accounts.")
def user_list(group_name: Optional[str], inactive: bool) -> None:
    """List print accounts and their balances."""
    with db_session.session_scope() as session:
        stmt = select(User).order_by(User.username)
        if group_name:
            stmt = stmt.where(User.group_name == group_name)
        if not inactive:
            stmt = stmt.where(User.is_active.is_(True))
        rows = [
            [
                u.username,
                u.group_name or "-",
                f"{u.pages_used}/{u.quota_limit}",
                u.remaining,
                "yes" if u.is_active else "no",
                "yes" if u.is_admin else "no",
            ]
            for u in session.scalars(stmt)
        ]
    _echo_table(["username", "group", "used", "remaining", "active", "admin"], rows)


@user.command("show")
@click.argument("username")
def user_show(username: str) -> None:
    """Show one account with its recent jobs."""
    settings = get_settings()
    with db_session.session_scope() as session:
        record = session.get(User, username)
        if record is None:
            raise click.ClickException(f"no such user: {username}")
        period_days = int(settings.get("quota.period_days", 30))
        click.echo(f"username      : {record.username}")
        click.echo(f"display name  : {record.display_name or '-'}")
        click.echo(f"email         : {record.email or '-'}")
        click.echo(f"group         : {record.group_name or '-'}")
        click.echo(f"quota         : {record.pages_used}/{record.quota_limit} pages")
        click.echo(f"remaining     : {record.remaining} pages")
        click.echo(f"period        : {record.period_start:%Y-%m-%d} -> {record.period_end(period_days):%Y-%m-%d}")
        click.echo(f"low threshold : {record.low_balance_threshold}")
        click.echo(f"active/admin  : {record.is_active}/{record.is_admin}")
        jobs = usage_rows(session, username=username)[:10]
        rows = [
            [
                j.submitted_at.strftime("%Y-%m-%d %H:%M"),
                j.printer,
                (j.title or "")[:30],
                j.status,
                j.actual_pages if j.actual_pages is not None else j.estimated_pages,
                f"{j.cost or 0:.2f}",
                (j.denial_reason or "")[:40],
            ]
            for j in jobs
        ]
    click.echo("")
    _echo_table(["when", "printer", "title", "status", "pages", "cost", "reason"], rows)


@user.command("set-quota")
@click.argument("username")
@click.argument("pages", type=int)
def user_set_quota(username: str, pages: int) -> None:
    """Set a user's page allowance."""
    if pages < 0:
        raise click.ClickException("quota must be zero or more")
    with db_session.session_scope() as session:
        record = session.get(User, username)
        if record is None:
            raise click.ClickException(f"no such user: {username}")
        before = record.quota_limit
        record.quota_limit = pages
        record_audit(session, _actor(), "user.set_quota", username, {"from": before, "to": pages})
    click.echo(f"{username}: quota {before} -> {pages}")


@user.command("set-password")
@click.argument("username")
@click.option("--password", default=None, help="Read from a prompt when omitted.")
def user_set_password(username: str, password: Optional[str]) -> None:
    """Set a portal password (local auth backend)."""
    password = password or getpass.getpass("new password: ")
    with db_session.session_scope() as session:
        record = session.get(User, username)
        if record is None:
            raise click.ClickException(f"no such user: {username}")
        record.password_hash = _hash_password(password)
        record_audit(session, _actor(), "user.set_password", username)
    click.echo(f"password updated for {username}")


@user.command("set-group")
@click.argument("username")
@click.argument("group_name")
def user_set_group(username: str, group_name: str) -> None:
    """Move a user into a group (use '-' to clear)."""
    with db_session.session_scope() as session:
        record = session.get(User, username)
        if record is None:
            raise click.ClickException(f"no such user: {username}")
        target = None if group_name == "-" else group_name
        if target and session.get(Group, target) is None:
            raise click.ClickException(f"no such group: {target}")
        record.group_name = target
        record_audit(session, _actor(), "user.set_group", username, {"group": target})
    click.echo(f"{username}: group -> {group_name}")


@user.command("enable")
@click.argument("username")
def user_enable(username: str) -> None:
    """Re-enable a disabled account."""
    _set_active(username, True)


@user.command("disable")
@click.argument("username")
def user_disable(username: str) -> None:
    """Disable an account (all its jobs are denied)."""
    _set_active(username, False)


def _set_active(username: str, active: bool) -> None:
    with db_session.session_scope() as session:
        record = session.get(User, username)
        if record is None:
            raise click.ClickException(f"no such user: {username}")
        record.is_active = active
        record_audit(session, _actor(), "user.enable" if active else "user.disable", username)
    click.echo(f"{username}: {'enabled' if active else 'disabled'}")


@user.command("reset")
@click.argument("username")
def user_reset(username: str) -> None:
    """Zero a user's usage and restart their period now."""
    with db_session.session_scope() as session:
        record = session.get(User, username)
        if record is None:
            raise click.ClickException(f"no such user: {username}")
        before = record.pages_used
        reset_user(session, record)
        record_audit(session, _actor(), "user.reset", username, {"pages_used_before": before})
    click.echo(f"{username}: usage reset (was {before} pages)")


@user.command("delete")
@click.argument("username")
@click.confirmation_option(prompt="Delete this user and all of their job history?")
def user_delete(username: str) -> None:
    """Delete an account and its job history."""
    with db_session.session_scope() as session:
        record = session.get(User, username)
        if record is None:
            raise click.ClickException(f"no such user: {username}")
        jobs = delete_user(session, record)
        record_audit(session, _actor(), "user.delete", username, {"jobs_deleted": jobs})
    click.echo(f"deleted {username}")


# ------------------------------------------------------------------------ group
@cli.group()
def group() -> None:
    """Manage groups and shared budgets."""


@group.command("add")
@click.argument("name")
@click.option("--budget", type=int, default=None, help="Shared page pool (omit for none).")
@click.option("--description", default=None)
def group_add(name: str, budget: Optional[int], description: Optional[str]) -> None:
    """Create a group."""
    with db_session.session_scope() as session:
        if session.get(Group, name) is not None:
            raise click.ClickException(f"group '{name}' already exists")
        session.add(Group(name=name, shared_quota=budget, description=description))
        record_audit(session, _actor(), "group.add", name, {"budget": budget})
    click.echo(f"created group '{name}'")


@group.command("list")
def group_list() -> None:
    """List groups, their budgets and usage."""
    with db_session.session_scope() as session:
        rows = []
        for record in session.scalars(select(Group).order_by(Group.name)):
            members = len(record.users)
            budget = "unlimited" if record.shared_quota is None else record.shared_quota
            remaining = "-" if record.remaining is None else record.remaining
            rows.append([record.name, members, budget, record.pages_used, remaining])
    _echo_table(["group", "members", "budget", "used", "remaining"], rows)


@group.command("set-budget")
@click.argument("name")
@click.argument("pages")
def group_set_budget(name: str, pages: str) -> None:
    """Set a group's shared pool ('none' to remove the pool)."""
    with db_session.session_scope() as session:
        record = session.get(Group, name)
        if record is None:
            raise click.ClickException(f"no such group: {name}")
        value = None if pages.lower() in ("none", "-", "unlimited") else int(pages)
        record.shared_quota = value
        record_audit(session, _actor(), "group.set_budget", name, {"budget": value})
    click.echo(f"{name}: budget -> {pages}")


@group.command("reset")
@click.argument("name")
def group_reset(name: str) -> None:
    """Zero a group's shared-pool usage."""
    with db_session.session_scope() as session:
        record = session.get(Group, name)
        if record is None:
            raise click.ClickException(f"no such group: {name}")
        before = record.pages_used
        record.pages_used = 0
        record.period_start = utcnow()
        record_audit(session, _actor(), "group.reset", name, {"pages_used_before": before})
    click.echo(f"{name}: usage reset (was {before} pages)")


# ---------------------------------------------------------------------- printer
@cli.group()
def printer() -> None:
    """Manage printers and their cost model."""


@printer.command("add")
@click.argument("name")
@click.option("--device-uri", default=None, help="The real device URI behind the quota wrapper.")
@click.option("--mono", type=float, default=None, help="Cost per mono page.")
@click.option("--color", type=float, default=None, help="Cost per colour page.")
@click.option("--duplex/--no-duplex", default=False)
@click.option("--duplex-discount", type=float, default=0.0, help="0.5 = duplex page costs half.")
@click.option("--description", default=None)
def printer_add(
    name: str,
    device_uri: Optional[str],
    mono: Optional[float],
    color: Optional[float],
    duplex: bool,
    duplex_discount: float,
    description: Optional[str],
) -> None:
    """Register a CUPS queue."""
    settings = get_settings()
    if not 0 <= duplex_discount < 1:
        raise click.ClickException("duplex discount must be >= 0 and < 1")
    with db_session.session_scope() as session:
        if session.get(Printer, name) is not None:
            raise click.ClickException(f"printer '{name}' already exists")
        session.add(
            Printer(
                name=name,
                description=description,
                real_device_uri=device_uri,
                cost_per_page_mono=(
                    mono if mono is not None else float(settings.get("printing.default_cost_per_page_mono", 0.0))
                ),
                cost_per_page_color=(
                    color if color is not None else float(settings.get("printing.default_cost_per_page_color", 0.0))
                ),
                supports_duplex=duplex,
                duplex_discount=duplex_discount,
            )
        )
        record_audit(session, _actor(), "printer.add", name, {"device_uri": device_uri})
    click.echo(f"registered printer '{name}'")


@printer.command("list")
def printer_list() -> None:
    """List registered printers."""
    with db_session.session_scope() as session:
        rows = [
            [
                p.name,
                p.real_device_uri or "-",
                f"{p.cost_per_page_mono:g}",
                f"{p.cost_per_page_color:g}",
                "yes" if p.supports_duplex else "no",
                "two-sided" if p.duplex_default else "one-sided",
                f"{p.duplex_discount:g}",
                "yes" if p.is_active else "no",
            ]
            for p in session.scalars(select(Printer).order_by(Printer.name))
        ]
    _echo_table(["printer", "device uri", "mono", "color", "duplex", "sides", "discount", "active"], rows)


@printer.command("sides")
@click.argument("name")
@click.argument("sides", type=click.Choice(["two-sided", "one-sided"]))
def printer_sides(name: str, sides: str) -> None:
    """Make a queue print two-sided (or one-sided) by default.

    Changes the CUPS queue, so run it as root or a member of lpadmin.
    """
    two_sided = sides == "two-sided"
    with db_session.session_scope() as session:
        record = session.get(Printer, name)
        if record is None:
            raise click.ClickException(f"no such printer: {name}")
        try:
            result = cups_queues.set_duplex_default(name, two_sided)
        except cups_queues.CupsError as exc:
            raise click.ClickException(str(exc)) from None
        record.duplex_default = two_sided
        if two_sided:
            record.supports_duplex = True
        record_audit(
            session, _actor(), "printer.duplex_default", name,
            {"two_sided": two_sided, "driver_options": result.ppd_settings},
        )
    click.echo(f"{name}: prints {sides} by default")
    if result.warning:
        click.echo(f"warning: {result.warning}", err=True)


@printer.command("set-cost")
@click.argument("name")
@click.option("--mono", type=float, default=None)
@click.option("--color", type=float, default=None)
@click.option("--duplex-discount", type=float, default=None)
def printer_set_cost(
    name: str, mono: Optional[float], color: Optional[float], duplex_discount: Optional[float]
) -> None:
    """Update a printer's cost model."""
    with db_session.session_scope() as session:
        record = session.get(Printer, name)
        if record is None:
            raise click.ClickException(f"no such printer: {name}")
        if mono is not None:
            record.cost_per_page_mono = mono
        if color is not None:
            record.cost_per_page_color = color
        if duplex_discount is not None:
            if not 0 <= duplex_discount < 1:
                raise click.ClickException("duplex discount must be >= 0 and < 1")
            record.duplex_discount = duplex_discount
        record_audit(
            session,
            _actor(),
            "printer.set_cost",
            name,
            {"mono": mono, "color": color, "duplex_discount": duplex_discount},
        )
    click.echo(f"{name}: cost model updated")


@printer.command("disable")
@click.argument("name")
def printer_disable(name: str) -> None:
    """Stop accepting jobs for a printer."""
    with db_session.session_scope() as session:
        record = session.get(Printer, name)
        if record is None:
            raise click.ClickException(f"no such printer: {name}")
        record.is_active = False
        record_audit(session, _actor(), "printer.disable", name)
    click.echo(f"{name}: disabled")


@printer.command("enable")
@click.argument("name")
def printer_enable(name: str) -> None:
    """Resume accepting jobs for a printer."""
    with db_session.session_scope() as session:
        record = session.get(Printer, name)
        if record is None:
            raise click.ClickException(f"no such printer: {name}")
        record.is_active = True
        record_audit(session, _actor(), "printer.enable", name)
    click.echo(f"{name}: enabled")


# ----------------------------------------------------------------------- policy
@cli.group()
def policy() -> None:
    """Manage print policies."""


@policy.command("add")
@click.option("--scope", type=click.Choice(PrintPolicy.SCOPES), required=True)
@click.option("--value", "scope_value", default=None, help="Username/group/printer for the scope.")
@click.option("--rule", "rule_type", type=click.Choice(PrintPolicy.RULES), required=True)
@click.option("--rule-value", default=None)
def policy_add(scope: str, scope_value: Optional[str], rule_type: str, rule_value: Optional[str]) -> None:
    """Add a policy rule."""
    if scope != "global" and not scope_value:
        raise click.ClickException(f"--value is required for scope '{scope}'")
    try:
        validate_rule(rule_type, rule_value if rule_value is not None else "true")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    with db_session.session_scope() as session:
        if scope == "user" and session.get(User, scope_value) is None:
            raise click.ClickException(f"no such user: {scope_value}")
        if scope == "group" and session.get(Group, scope_value) is None:
            raise click.ClickException(f"no such group: {scope_value}")
        if scope == "printer" and session.get(Printer, scope_value) is None:
            raise click.ClickException(f"no such printer: {scope_value}")
        record = PrintPolicy(
            scope_type=scope,
            scope_value=None if scope == "global" else scope_value,
            rule_type=rule_type,
            rule_value=rule_value if rule_value is not None else "true",
        )
        session.add(record)
        session.flush()
        record_audit(
            session,
            _actor(),
            "policy.add",
            f"{scope}:{scope_value}",
            {"rule": rule_type, "value": rule_value, "id": record.id},
        )
        policy_id = record.id
    click.echo(f"added policy #{policy_id}")


@policy.command("list")
def policy_list() -> None:
    """List policy rules."""
    with db_session.session_scope() as session:
        rows = [
            [
                p.id,
                p.scope_type,
                p.scope_value or "-",
                p.rule_type,
                p.rule_value or "-",
                "yes" if p.is_active else "no",
            ]
            for p in session.scalars(select(PrintPolicy).order_by(PrintPolicy.id))
        ]
    _echo_table(["id", "scope", "value", "rule", "rule value", "active"], rows)


@policy.command("remove")
@click.argument("policy_id", type=int)
def policy_remove(policy_id: int) -> None:
    """Delete a policy rule."""
    with db_session.session_scope() as session:
        record = session.get(PrintPolicy, policy_id)
        if record is None:
            raise click.ClickException(f"no such policy: {policy_id}")
        session.delete(record)
        record_audit(session, _actor(), "policy.remove", str(policy_id))
    click.echo(f"removed policy #{policy_id}")


# ---------------------------------------------------------------------- reports
@cli.command("usage")
@click.option("--days", type=int, default=30, help="Look-back window.")
@click.option("--user", "username", default=None)
@click.option("--printer", "printer_name", default=None)
@click.option("--status", "statuses", multiple=True)
@click.option("--csv", "csv_path", type=click.Path(), default=None, help="Write CSV here ('-' for stdout).")
def usage(
    days: int,
    username: Optional[str],
    printer_name: Optional[str],
    statuses: tuple[str, ...],
    csv_path: Optional[str],
) -> None:
    """Report print usage, optionally as CSV."""
    since = utcnow() - dt.timedelta(days=days)
    with db_session.session_scope() as session:
        jobs = usage_rows(
            session,
            since=since,
            username=username,
            printer=printer_name,
            statuses=list(statuses) or None,
        )
        records = [
            {
                "submitted_at": j.submitted_at.isoformat(),
                "username": j.username,
                "printer": j.printer,
                "title": j.title or "",
                "copies": j.copies,
                "color": int(j.is_color),
                "duplex": int(j.is_duplex),
                "estimated_pages": j.estimated_pages,
                "actual_pages": j.actual_pages if j.actual_pages is not None else "",
                "charged_pages": j.charged_pages,
                "cost": f"{j.cost or 0:.4f}",
                "status": j.status,
                "denial_reason": j.denial_reason or "",
            }
            for j in jobs
        ]
    if csv_path:
        stream = sys.stdout if csv_path == "-" else open(csv_path, "w", newline="")
        try:
            writer = csv.DictWriter(stream, fieldnames=list(records[0].keys()) if records else
                                    ["submitted_at", "username", "printer", "title", "copies",
                                     "color", "duplex", "estimated_pages", "actual_pages",
                                     "charged_pages", "cost", "status", "denial_reason"])
            writer.writeheader()
            writer.writerows(records)
        finally:
            if stream is not sys.stdout:
                stream.close()
                click.echo(f"wrote {len(records)} rows to {csv_path}")
        return

    rows = [
        [
            r["submitted_at"][:16].replace("T", " "),
            r["username"],
            r["printer"],
            r["title"][:28],
            r["status"],
            r["charged_pages"],
            r["cost"],
        ]
        for r in records
    ]
    _echo_table(["when", "user", "printer", "title", "status", "pages", "cost"], rows)
    total_pages = sum(r["charged_pages"] for r in records)
    total_cost = sum(float(r["cost"]) for r in records)
    click.echo("")
    click.echo(f"{len(records)} jobs, {total_pages} pages, {total_cost:.2f} total cost")


@cli.command("reset-periods")
@click.option("--dry-run", is_flag=True, help="Report what would roll without changing anything.")
def reset_periods(dry_run: bool) -> None:
    """Roll every elapsed rolling quota period (run by the systemd timer)."""
    if dry_run:
        settings = get_settings()
        period_days = int(settings.get("quota.period_days", 30))
        now = utcnow()
        with db_session.session_scope() as session:
            due = [
                u.username
                for u in session.scalars(select(User))
                if u.period_end(period_days) <= now
            ]
            session.rollback()
        click.echo(f"{len(due)} user period(s) due: {', '.join(due) or '-'}")
        return
    with db_session.session_scope() as session:
        result = reset_expired_periods(session)
        record_audit(session, _actor(), "quota.reset_periods", None, result)
    click.echo(f"rolled {result['users']} user period(s), {result['groups']} group period(s)")


@cli.command("audit")
@click.option("--limit", type=int, default=25)
def audit(limit: int) -> None:
    """Show the most recent admin actions."""
    with db_session.session_scope() as session:
        stmt = select(AdminAuditLog).order_by(AdminAuditLog.timestamp.desc()).limit(limit)
        rows = [
            [
                a.timestamp.strftime("%Y-%m-%d %H:%M"),
                a.admin_user,
                a.action,
                a.target or "-",
                (a.details or "")[:48],
                a.source or "-",
            ]
            for a in session.scalars(stmt)
        ]
    _echo_table(["when", "admin", "action", "target", "details", "source"], rows)


def main() -> int:  # pragma: no cover - console-script shim
    cli(standalone_mode=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    cli()
