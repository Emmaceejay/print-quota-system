"""Entry point for the quota reset systemd timer."""

from __future__ import annotations

import sys

from ..core.logging import setup_logging
from ..db import session as db_session
from ..services.audit import record_audit
from ..services.quota import reset_expired_periods


def main(argv: list[str] | None = None) -> int:
    log = setup_logging("reset")
    with db_session.session_scope() as session:
        result = reset_expired_periods(session)
        record_audit(session, "system", "quota.reset_periods", None, result, source="timer")
    log.info("quota periods rolled", extra=result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
