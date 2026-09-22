"""Structured logging setup.

Logs go to journald when the systemd bindings are available (so
``journalctl -u quota-accounting`` gives real observability) and fall back
to stderr otherwise. Every record carries the component name.
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


class _KeyValueFormatter(logging.Formatter):
    """Human-readable but parseable ``key=value`` tail for extra fields."""

    _RESERVED = set(
        logging.LogRecord("", 0, "", 0, "", (), None).__dict__
    ) | {"message", "asctime", "taskName"}

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in self._RESERVED
        }
        if extras:
            tail = " ".join(f"{k}={v!r}" for k, v in sorted(extras.items()))
            return f"{base} {tail}"
        return base


def setup_logging(component: str, level: str | None = None, use_journald: bool | None = None) -> logging.Logger:
    """Configure root logging once and return the component logger."""
    global _CONFIGURED
    from .config import get_settings

    settings = get_settings()
    level_name = (level or os.environ.get("PRINTQUOTA_LOG_LEVEL") or settings.get("logging.level", "INFO")).upper()
    want_journald = settings.get("logging.use_journald", True) if use_journald is None else use_journald

    if not _CONFIGURED:
        handler: logging.Handler | None = None
        if want_journald:
            try:  # pragma: no cover - depends on host packages
                from systemd.journal import JournalHandler  # type: ignore

                handler = JournalHandler(SYSLOG_IDENTIFIER="printquota")
                handler.setFormatter(_KeyValueFormatter("%(name)s: %(message)s"))
            except Exception:
                handler = None
        if handler is None:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(
                _KeyValueFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            )
        root = logging.getLogger("printquota")
        root.handlers.clear()
        root.addHandler(handler)
        root.propagate = False
        _CONFIGURED = True

    logging.getLogger("printquota").setLevel(level_name)
    return logging.getLogger(f"printquota.{component}")


def get_logger(component: str) -> logging.Logger:
    """Return a component logger without forcing handler setup."""
    return logging.getLogger(f"printquota.{component}")
