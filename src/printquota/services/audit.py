"""Admin audit trail."""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..db.models import AdminAuditLog


def record_audit(
    session: Session,
    admin_user: str,
    action: str,
    target: Optional[str] = None,
    details: Optional[Any] = None,
    source: str = "cli",
) -> AdminAuditLog:
    """Append one row to the audit log. Never raises on unserialisable details."""
    if details is not None and not isinstance(details, str):
        try:
            details = json.dumps(details, default=str, sort_keys=True)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            details = repr(details)
    entry = AdminAuditLog(
        admin_user=admin_user,
        action=action,
        target=target,
        details=details,
        source=source,
    )
    session.add(entry)
    return entry
