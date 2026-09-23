"""Bulk user import for the web console.

Input is CSV text (an uploaded file or rows pasted into a text box). With a
header row, columns are matched by name in any order; without one, the
columns are read in :data:`COLUMNS` order, so a plain list of usernames
(one per line) works too.

Import is two-phase: :func:`plan_import` works out what every row would do
without writing anything (the console shows this as a preview), and
:func:`apply_import` re-plans against the current database and applies it.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..db.models import Group, User
from .audit import record_audit

#: Column order for header-less input, and the names the template uses.
COLUMNS = ("username", "display_name", "email", "group", "quota", "threshold", "password", "admin", "active")

_ALIASES = {
    "user": "username",
    "login": "username",
    "name": "display_name",
    "full_name": "display_name",
    "fullname": "display_name",
    "display": "display_name",
    "mail": "email",
    "e-mail": "email",
    "group_name": "group",
    "department": "group",
    "quota_limit": "quota",
    "pages": "quota",
    "low_balance_threshold": "threshold",
    "is_admin": "admin",
    "is_active": "active",
    "enabled": "active",
}

#: Same rule the setup wizard uses: no whitespace or path/shell metacharacters.
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@\\\-]{1,128}$")
MAX_ROWS = 5000
_TRUE = {"1", "true", "yes", "y", "on", "x"}
_FALSE = {"0", "false", "no", "n", "off"}

TEMPLATE_CSV = (
    ",".join(COLUMNS) + "\n"
    "ada,Ada Obi,ada@example.com,finance,500,50,,no,yes\n"
    "tunde,Tunde Bello,tunde@example.com,engineering,800,,ChangeMe123,no,yes\n"
)


@dataclass
class ImportOptions:
    default_quota: int
    default_threshold: int
    default_group: Optional[str] = None
    create_missing_groups: bool = False
    update_existing: bool = False


@dataclass
class PlannedRow:
    line: int
    username: str
    action: str  # create | update | skip | error
    message: str = ""
    values: dict = field(default_factory=dict)


@dataclass
class ImportPlan:
    rows: list[PlannedRow]
    groups_to_create: list[str]

    def count(self, action: str) -> int:
        return sum(1 for row in self.rows if row.action == action)

    @property
    def has_changes(self) -> bool:
        return any(row.action in ("create", "update") for row in self.rows)


def decode_upload(data: bytes) -> str:
    """Decode an uploaded file (UTF-8 with or without BOM, else Latin-1)."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _normalise_header(cell: str) -> str:
    key = cell.strip().lower().replace(" ", "_")
    return _ALIASES.get(key, key)


def parse(text: str) -> list[tuple[int, dict[str, str]]]:
    """Split CSV text into ``(line_number, {column: value})`` records."""
    text = (text or "").strip("﻿")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t") if sample.strip() else csv.excel
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    records: list[tuple[int, dict[str, str]]] = []
    header: Optional[list[str]] = None
    for index, row in enumerate(reader, start=1):
        if not row or all(not cell.strip() for cell in row) or row[0].lstrip().startswith("#"):
            continue
        if header is None and not records:
            normalised = [_normalise_header(cell) for cell in row]
            if "username" in normalised:
                header = normalised
                continue
            header = list(COLUMNS)
        record = {
            header[i]: cell.strip()
            for i, cell in enumerate(row)
            if i < len(header) and header[i] in COLUMNS
        }
        records.append((index, record))
        if len(records) > MAX_ROWS:
            raise ValueError(f"Too many rows: import at most {MAX_ROWS} users at a time.")
    return records


def _flag(raw: str, column: str) -> Optional[bool]:
    value = raw.strip().lower()
    if not value:
        return None
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{column} must be yes or no, not '{raw}'")


def _count(raw: str, column: str) -> Optional[int]:
    value = raw.strip()
    if not value:
        return None
    if not value.isdigit():
        raise ValueError(f"{column} must be a whole number of pages, not '{raw}'")
    return int(value)


def plan_import(session: Session, text: str, options: ImportOptions) -> ImportPlan:
    """Work out what importing ``text`` would do, without writing anything."""
    rows: list[PlannedRow] = []
    seen: set[str] = set()
    groups_to_create: list[str] = []

    for line, record in parse(text):
        username = record.get("username", "").strip()
        planned = PlannedRow(line=line, username=username or "(blank)", action="error")
        rows.append(planned)
        try:
            if not username:
                raise ValueError("username is empty")
            if not USERNAME_RE.fullmatch(username):
                raise ValueError("username may only contain letters, digits and . _ @ \\ -")
            if username.lower() in seen:
                raise ValueError("appears more than once in this import")
            seen.add(username.lower())

            quota = _count(record.get("quota", ""), "quota")
            threshold = _count(record.get("threshold", ""), "threshold")
            is_admin = _flag(record.get("admin", ""), "admin")
            is_active = _flag(record.get("active", ""), "active")
            email = record.get("email", "").strip()
            if email and "@" not in email:
                raise ValueError(f"'{email}' is not an email address")
            password = record.get("password", "")
            if password and len(password) < 8:
                raise ValueError("password must be at least 8 characters")

            group = record.get("group", "").strip() or (options.default_group or "")
            if group:
                if len(group) > 128:
                    raise ValueError("group name is too long")
                if session.get(Group, group) is None and group not in groups_to_create:
                    if not options.create_missing_groups:
                        raise ValueError(
                            f"group '{group}' does not exist (tick 'create missing groups' to add it)"
                        )
                    groups_to_create.append(group)

            values = {
                "display_name": record.get("display_name", "").strip() or None,
                "email": email or None,
                "group": group or None,
                "quota": quota,
                "threshold": threshold,
                "password": password or None,
                "admin": is_admin,
                "active": is_active,
            }
            existing = session.get(User, username)
            if existing is None:
                planned.action = "create"
                planned.message = _describe_create(values, options)
            elif options.update_existing:
                changes = _changes(existing, values)
                if changes:
                    planned.action = "update"
                    planned.message = "; ".join(changes)
                else:
                    planned.action = "skip"
                    planned.message = "already up to date"
            else:
                planned.action = "skip"
                planned.message = "already exists (tick 'update existing users' to change it)"
            planned.values = values
        except ValueError as exc:
            planned.action = "error"
            planned.message = str(exc)
    return ImportPlan(rows=rows, groups_to_create=groups_to_create)


def _describe_create(values: dict, options: ImportOptions) -> str:
    quota = values["quota"] if values["quota"] is not None else options.default_quota
    parts = [f"quota {quota}"]
    if values["group"]:
        parts.append(f"group {values['group']}")
    if values["admin"]:
        parts.append("administrator")
    if values["password"]:
        parts.append("portal password set")
    if values["active"] is False:
        parts.append("disabled")
    return ", ".join(parts)


def _changes(user: User, values: dict) -> list[str]:
    """Human-readable differences an update would make (blank cells change nothing)."""
    changes = []
    if values["display_name"] and values["display_name"] != user.display_name:
        changes.append(f"name -> {values['display_name']}")
    if values["email"] and values["email"] != user.email:
        changes.append(f"email -> {values['email']}")
    if values["group"] and values["group"] != user.group_name:
        changes.append(f"group {user.group_name or '-'} -> {values['group']}")
    if values["quota"] is not None and values["quota"] != user.quota_limit:
        changes.append(f"quota {user.quota_limit} -> {values['quota']}")
    if values["threshold"] is not None and values["threshold"] != user.low_balance_threshold:
        changes.append(f"threshold {user.low_balance_threshold} -> {values['threshold']}")
    if values["admin"] is not None and values["admin"] != user.is_admin:
        changes.append("grant admin" if values["admin"] else "revoke admin")
    if values["active"] is not None and values["active"] != user.is_active:
        changes.append("enable" if values["active"] else "disable")
    if values["password"]:
        changes.append("new password")
    return changes


def apply_import(
    session: Session, text: str, options: ImportOptions, actor: str
) -> ImportPlan:
    """Apply an import. Rows in error are skipped; everything else is written."""
    from ..api.auth import hash_password

    plan = plan_import(session, text, options)
    for name in plan.groups_to_create:
        session.add(Group(name=name))
        record_audit(session, actor, "group.add", name, {"via": "import"}, source="web")
    session.flush()

    for row in plan.rows:
        values = row.values
        if row.action == "create":
            session.add(
                User(
                    username=row.username,
                    display_name=values["display_name"] or row.username,
                    email=values["email"],
                    group_name=values["group"],
                    quota_limit=values["quota"] if values["quota"] is not None else options.default_quota,
                    low_balance_threshold=(
                        values["threshold"] if values["threshold"] is not None else options.default_threshold
                    ),
                    is_admin=bool(values["admin"]),
                    is_active=values["active"] is not False,
                    password_hash=hash_password(values["password"]) if values["password"] else None,
                )
            )
        elif row.action == "update":
            user = session.get(User, row.username)
            if values["display_name"]:
                user.display_name = values["display_name"]
            if values["email"]:
                user.email = values["email"]
            if values["group"]:
                user.group_name = values["group"]
            if values["quota"] is not None:
                user.quota_limit = values["quota"]
            if values["threshold"] is not None:
                user.low_balance_threshold = values["threshold"]
            if values["admin"] is not None and not (row.username == actor and not values["admin"]):
                user.is_admin = values["admin"]
            if values["active"] is not None and not (row.username == actor and not values["active"]):
                user.is_active = values["active"]
            if values["password"]:
                user.password_hash = hash_password(values["password"])

    record_audit(
        session,
        actor,
        "user.import",
        f"{plan.count('create')} created, {plan.count('update')} updated",
        {
            "created": [r.username for r in plan.rows if r.action == "create"],
            "updated": [r.username for r in plan.rows if r.action == "update"],
            "errors": plan.count("error"),
            "groups_created": plan.groups_to_create,
        },
        source="web",
    )
    return plan


def default_options(**overrides) -> ImportOptions:
    settings = get_settings()
    options = ImportOptions(
        default_quota=int(settings.get("quota.default_limit", 500)),
        default_threshold=int(settings.get("quota.default_low_balance_threshold", 50)),
    )
    for key, value in overrides.items():
        setattr(options, key, value)
    return options


def export_csv(users: list[User]) -> str:
    """Users in the import format (no passwords), for round-tripping."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(COLUMNS)
    for user in users:
        writer.writerow(
            [
                user.username,
                user.display_name or "",
                user.email or "",
                user.group_name or "",
                user.quota_limit,
                user.low_balance_threshold,
                "",
                "yes" if user.is_admin else "no",
                "yes" if user.is_active else "no",
            ]
        )
    return buffer.getvalue()
