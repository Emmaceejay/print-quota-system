"""Settings an administrator edits from the web console.

The values live in the ``app_settings`` table and are layered into
:func:`printquota.core.config.get_settings` between the YAML file and the
environment (see that module for the full precedence). This module owns the
field definitions the Settings page renders, validation, and persistence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..core.config import (
    DEFAULTS,
    RUNTIME_KEYS,
    env_overridden_keys,
    get_base_settings,
    get_settings,
    invalidate_settings,
    load_settings,
)
from ..db.models import AppSetting, utcnow
from .audit import record_audit


@dataclass(frozen=True)
class Field:
    """One editable setting, as the console presents it."""

    key: str
    label: str
    kind: str  # int | float | bool | choice | text | secret | email | url
    section: str
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    #: Upper bound is exclusive (used for the duplex discount).
    maximum_exclusive: bool = False


FIELDS: tuple[Field, ...] = (
    Field("quota.default_limit", "Default quota (pages)", "int", "Quotas",
          "Allowance given to new users when none is specified.", minimum=0),
    Field("quota.period_days", "Quota period (days)", "int", "Quotas",
          "Length of each user's rolling period.", minimum=1, maximum=3660),
    Field("quota.default_low_balance_threshold", "Default low-balance threshold (pages)", "int", "Quotas",
          "New users get a low-balance alert when this many pages remain.", minimum=0),
    Field("quota.enforcement", "Enforcement mode", "choice", "Quotas",
          "Strict denies jobs that do not fit the balance. Soft lets them print and lets "
          "the balance go negative (group budgets are not checked in soft mode).",
          choices=("strict", "soft")),
    Field("quota.enforce_group_budget", "Enforce group budgets", "bool", "Quotas",
          "A job must also fit the group's shared pool."),
    Field("printing.currency", "Currency label", "text", "Costs",
          "Shown next to every cost figure, e.g. NGN, USD."),
    Field("printing.default_cost_per_page_mono", "Default cost per mono page", "float", "Costs",
          "Used when a printer's own mono rate is 0 or the printer is not registered.", minimum=0),
    Field("printing.default_cost_per_page_color", "Default cost per colour page", "float", "Costs",
          "Used when a printer's own colour rate is 0 or the printer is not registered.", minimum=0),
    Field("printing.default_duplex_discount", "Default duplex discount", "float", "Costs",
          "Only for jobs on unregistered printers. 0.5 means a duplex page costs half.",
          minimum=0, maximum=1, maximum_exclusive=True),
    Field("alerts.enabled", "Send alerts", "bool", "Alerts",
          "Low-balance and over-quota notifications."),
    Field("alerts.channel", "Delivery channel", "choice", "Alerts",
          "Email goes to each user's email address; webhook posts JSON to one URL.",
          choices=("email", "webhook", "none")),
    Field("alerts.cooldown_hours", "Cooldown (hours)", "int", "Alerts",
          "Minimum gap between two alerts of the same type to the same user. 0 disables it.",
          minimum=0),
    Field("alerts.webhook_url", "Webhook URL", "url", "Alerts",
          "Receives a JSON POST per alert when the channel is webhook."),
    Field("alerts.smtp.host", "SMTP server", "text", "Email (SMTP)",
          "Leave empty to disable email delivery."),
    Field("alerts.smtp.port", "SMTP port", "int", "Email (SMTP)", minimum=1, maximum=65535),
    Field("alerts.smtp.use_tls", "Use STARTTLS", "bool", "Email (SMTP)"),
    Field("alerts.smtp.user", "SMTP username", "text", "Email (SMTP)",
          "Leave empty if the server does not require a login."),
    Field("alerts.smtp.password", "SMTP password", "secret", "Email (SMTP)",
          "Stored in the printquota database. Leave blank to keep the current password."),
    Field("alerts.smtp.from_address", "From address", "email", "Email (SMTP)"),
)

FIELDS_BY_KEY: dict[str, Field] = {f.key: f for f in FIELDS}
SECTIONS: tuple[str, ...] = tuple(dict.fromkeys(f.section for f in FIELDS))

_TRUE = {"1", "true", "yes", "on"}


def read_overrides(engine: Engine) -> dict[str, Any]:
    """Return ``{key: value}`` saved from the console (empty if the table is missing)."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(AppSetting.key, AppSetting.value)).all()
    except Exception:
        # Table not created yet (pre-migration) or database unreachable: the
        # file/env settings still apply, and the caller will surface any real
        # database problem on its own query.
        return {}
    out: dict[str, Any] = {}
    for key, raw in rows:
        if key not in RUNTIME_KEYS:
            continue
        try:
            out[key] = json.loads(raw)
        except (TypeError, ValueError):
            continue
    return out


def _number(text: str, whole: bool) -> float:
    """Parse a number the way people type it: ``1,000``, ``0,5``, ``500.0``."""
    cleaned = text.replace(" ", "").replace("\u00a0", "")
    if "," in cleaned and "." not in cleaned and not whole and cleaned.count(",") == 1:
        cleaned = cleaned.replace(",", ".")  # decimal comma: 0,5
    else:
        cleaned = cleaned.replace(",", "")  # thousands separator: 1,000
    return float(cleaned)


def coerce(key: str, raw: Any) -> Any:
    """Validate a submitted value and convert it to the setting's type.

    Raises ``ValueError`` with a message fit to show next to the field.
    """
    spec = FIELDS_BY_KEY.get(key)
    if spec is None:
        raise ValueError(f"{key} cannot be changed from the console")
    text = "" if raw is None else str(raw).strip()

    if spec.kind == "bool":
        return text.lower() in _TRUE
    if spec.kind in ("int", "float"):
        if not text:
            raise ValueError("enter a number")
        try:
            number = _number(text, whole=spec.kind == "int")
        except ValueError:
            raise ValueError(f"'{text}' is not a number") from None
        if spec.kind == "int":
            if not number.is_integer():
                raise ValueError("enter a whole number")
            value: Any = int(number)
        else:
            value = number
    elif spec.kind == "choice":
        if text not in spec.choices:
            raise ValueError(f"choose one of {', '.join(spec.choices)}")
        return text
    else:
        if spec.kind == "email" and text and ("@" not in text or " " in text):
            raise ValueError(f"'{text}' is not an email address")
        if spec.kind == "url" and text and not text.lower().startswith(("http://", "https://")):
            raise ValueError(f"'{text}' is not a web address; it must start with http:// or https://")
        if len(text) > 500:
            raise ValueError("too long (500 characters at most)")
        return text

    if spec.minimum is not None and value < spec.minimum:
        raise ValueError(f"must be at least {spec.minimum:g}")
    if spec.maximum is not None:
        too_big = value >= spec.maximum if spec.maximum_exclusive else value > spec.maximum
        if too_big:
            bound = "below" if spec.maximum_exclusive else "at most"
            raise ValueError(f"must be {bound} {spec.maximum:g}")
    return value


def _unchanged(spec: Field, raw: Any, current: Any) -> bool:
    """True when the submitted text is just the current value shown back.

    Fields the administrator did not touch are never re-validated, so a
    questionable value elsewhere (from the config file, say) can never block
    saving an unrelated change.
    """
    text = "" if raw is None else str(raw).strip()
    if spec.kind == "bool":
        return (text.lower() in _TRUE) == bool(current)
    if spec.kind in ("int", "float"):
        try:
            return current is not None and _number(text, whole=spec.kind == "int") == float(current)
        except (TypeError, ValueError):
            return False
    return text == ("" if current is None else str(current).strip())


@dataclass
class SaveResult:
    """Outcome of saving the settings form."""

    changed: list[str]
    #: key -> message for fields that could not be saved (everything else was).
    errors: dict[str, str]


@dataclass
class FieldState:
    """What the Settings page needs to render one field."""

    spec: Field
    value: Any
    origin: str  # default | file | console | environment
    locked: bool
    saved: bool = False


def describe(session: Session) -> list[FieldState]:
    """Current effective value and origin of every editable setting."""
    stored = {row.key: row for row in session.scalars(select(AppSetting))}
    effective = get_settings()
    file_only = load_settings()  # defaults + file + env, without console values
    pinned = env_overridden_keys()
    states: list[FieldState] = []
    for spec in FIELDS:
        value = effective.get(spec.key)
        if spec.key in pinned:
            origin = "environment"
        elif spec.key in stored:
            origin = "console"
        elif file_only.get(spec.key) != _dig(DEFAULTS, spec.key):
            origin = "file"
        else:
            origin = "default"
        states.append(
            FieldState(spec=spec, value=value, origin=origin, locked=spec.key in pinned,
                       saved=spec.key in stored)
        )
    return states


def _dig(data: dict, dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def save(session: Session, submitted: dict[str, Any], actor: str) -> SaveResult:
    """Store every valid change; report the invalid ones per field.

    Untouched fields are skipped without validation, environment-pinned keys
    are ignored and a blank secret means "keep the current one". One bad
    field therefore never stops the others from being saved.
    """
    pinned = env_overridden_keys()
    current = get_settings()
    pending: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, raw in submitted.items():
        spec = FIELDS_BY_KEY.get(key)
        if spec is None or key in pinned:
            continue
        if spec.kind == "secret":
            if raw is None or str(raw) == "":
                continue
        elif _unchanged(spec, raw, current.get(key)):
            continue
        try:
            value = coerce(key, raw)
        except ValueError as exc:
            errors[key] = str(exc)
            continue
        if value != current.get(key):
            pending[key] = value

    now = utcnow()
    for key, value in pending.items():
        row = session.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key)
            session.add(row)
        row.value = json.dumps(value)
        row.updated_by = actor
        row.updated_at = now
    if pending:
        record_audit(
            session,
            actor,
            "settings.update",
            None,
            {k: ("********" if FIELDS_BY_KEY[k].kind == "secret" else v) for k, v in pending.items()},
            source="web",
        )
        session.flush()
        invalidate_settings()
    return SaveResult(changed=sorted(pending), errors=errors)


def revert(session: Session, key: str, actor: str) -> bool:
    """Remove a console value so the file/default applies again."""
    row = session.get(AppSetting, key)
    if row is None:
        return False
    session.delete(row)
    record_audit(session, actor, "settings.revert", key, source="web")
    session.flush()
    invalidate_settings()
    return True


def config_summary() -> dict[str, str]:
    """Read-only facts about this installation, for the Settings page."""
    base = get_base_settings()
    url = str(base.get("database.url", ""))
    if "@" in url and "://" in url:  # hide credentials in a PostgreSQL URL
        scheme, rest = url.split("://", 1)
        url = f"{scheme}://****@{rest.split('@', 1)[1]}"
    return {
        "Configuration file": base.source or "defaults",
        "Database": url,
        "Sign-in backend": str(base.get("api.auth_backend", "local")),
        "CUPS page log": str(base.get("printing.page_log", "")),
        "CUPS backend directory": str(base.get("printing.real_backend_dir", "")),
        "Session lifetime (hours)": f"{int(base.get('api.session_max_age', 28800)) / 3600:g}",
    }
