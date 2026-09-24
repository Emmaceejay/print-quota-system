"""Post-print accounting daemon.

CUPS writes one ``page_log`` line per imaged page, which is the only
authoritative page count: the driver may compose, N-up or duplex the job
after the backend has already made its pre-flight estimate. This daemon
reads new ``page_log`` lines, aggregates them per (printer, CUPS job id),
and reconciles the matching :class:`~printquota.db.models.PrintJob` row.

State (inode + byte offset) is kept in a small JSON file so restarts resume
where they left off and a log rotation is detected rather than replayed.

Reconciliation is idempotent -- a job already marked ``reconciled`` is never
charged twice, so replaying an old log is safe.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import select

from ..core.config import get_settings
from ..core.logging import get_logger, setup_logging
from ..db import session as db_session
from ..db.models import PrintJob
from ..notifications.alerts import notify_balance_state
from ..services.quota import charge_job

log = get_logger("accounting.daemon")

DEFAULT_STATE_PATH = "/var/lib/printquota/accounting.state"

#: Default CUPS PageLogFormat:
#: %p %u %j %T %P %C %{job-billing} %{job-originating-host-name} %{job-name} %{media} %{sides}
_LINE_RE = re.compile(
    r"^(?P<printer>\S+)\s+(?P<user>\S+)\s+(?P<job>\d+)\s+"
    r"\[(?P<when>[^\]]+)\]\s+(?P<page>\S+)\s+(?P<copies>\S+)"
    r"(?:\s+(?P<rest>.*))?$"
)


@dataclass
class JobTally:
    """Accumulated page_log facts for one CUPS job."""

    printer: str
    username: str
    cups_job_id: int
    pages: int = 0
    sides_values: set[str] = field(default_factory=set)
    #: Page count from a ``total`` summary line, when CUPS writes one.
    total: Optional[int] = None

    @property
    def billed_pages(self) -> int:
        """Per-page lines when present; otherwise the summary total."""
        if self.pages:
            return self.pages
        return self.total or 0

    @property
    def is_duplex(self) -> bool:
        return any(value.startswith("two-sided") for value in self.sides_values)


def parse_page_log_line(line: str) -> Optional[JobTally]:
    """Parse one ``page_log`` line into a single-page tally, or ``None``."""
    match = _LINE_RE.match(line.strip())
    if not match:
        return None
    try:
        job_id = int(match.group("job"))
    except ValueError:
        return None
    if match.group("page") == "total":
        # A summary line: the copies column holds the job's total impressions.
        # It duplicates per-page lines when those exist, so it is only used
        # when they do not (see JobTally.billed_pages).
        try:
            total = max(int(match.group("copies")), 0)
        except (TypeError, ValueError):
            return None
        return JobTally(
            printer=match.group("printer"),
            username=match.group("user"),
            cups_job_id=job_id,
            total=total,
        )
    try:
        copies = max(int(match.group("copies")), 1)
    except (TypeError, ValueError):
        copies = 1
    tally = JobTally(
        printer=match.group("printer"),
        username=match.group("user"),
        cups_job_id=job_id,
        pages=copies,
    )
    rest = match.group("rest") or ""
    for token in rest.split():
        if token.startswith("two-sided") or token == "one-sided":
            tally.sides_values.add(token)
    return tally


def aggregate(lines: Iterable[str]) -> dict[tuple[str, int], JobTally]:
    """Fold page_log lines into per-job tallies."""
    tallies: dict[tuple[str, int], JobTally] = {}
    for line in lines:
        parsed = parse_page_log_line(line)
        if parsed is None:
            continue
        key = (parsed.printer, parsed.cups_job_id)
        existing = tallies.get(key)
        if existing is None:
            tallies[key] = parsed
        else:
            existing.pages += parsed.pages
            existing.sides_values |= parsed.sides_values
            if parsed.total is not None:
                existing.total = parsed.total
    return tallies


class LogCursor:
    """Tracks the read position in ``page_log`` across restarts and rotations."""

    def __init__(self, log_path: str | Path, state_path: str | Path) -> None:
        self.log_path = Path(log_path)
        self.state_path = Path(state_path)
        self.inode: Optional[int] = None
        self.offset: int = 0
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text())
            self.inode = data.get("inode")
            self.offset = int(data.get("offset", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            self.inode, self.offset = None, 0

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"inode": self.inode, "offset": self.offset}))
        os.replace(tmp, self.state_path)

    def read_new_lines(self) -> list[str]:
        """Return lines written since the last read; handles rotation."""
        if not self.log_path.exists():
            return []
        stat = self.log_path.stat()
        if self.inode is not None and stat.st_ino != self.inode:
            log.info("page_log rotated; starting from the top of the new file")
            self.offset = 0
        elif stat.st_size < self.offset:
            log.info("page_log truncated; restarting from offset 0")
            self.offset = 0
        self.inode = stat.st_ino
        with self.log_path.open("r", errors="replace") as handle:
            handle.seek(self.offset)
            lines = handle.readlines()
            # Drop a trailing partial line; it will be complete next pass.
            if lines and not lines[-1].endswith("\n"):
                partial = lines.pop()
                self.offset = handle.tell() - len(partial.encode("utf-8", "replace"))
            else:
                self.offset = handle.tell()
        return lines


def reconcile_tallies(tallies: dict[tuple[str, int], JobTally]) -> int:
    """Apply tallies to the datastore. Returns the number of jobs reconciled."""
    if not tallies:
        return 0
    settings = get_settings()
    reconciled = 0
    with db_session.session_scope() as session:
        for (printer, job_id), tally in tallies.items():
            stmt = (
                select(PrintJob)
                .where(PrintJob.cups_job_id == job_id)
                .where(PrintJob.printer == printer)
                .where(PrintJob.reconciled.is_(False))
                .where(PrintJob.status == PrintJob.STATUS_ALLOWED)
                .order_by(PrintJob.submitted_at.desc())
            )
            job = session.scalars(stmt).first()
            if job is None:
                log.warning(
                    "page_log entry with no matching allowed job",
                    extra={"printer": printer, "job": job_id, "pages": tally.billed_pages},
                )
                continue
            if tally.billed_pages <= 0:
                continue
            if tally.is_duplex:
                job.is_duplex = True
            charge_job(session, job, tally.billed_pages, settings=settings)
            reconciled += 1
            notify_balance_state(session, job.username, settings=settings)
    return reconciled


def run_once(log_path: str | Path, state_path: str | Path) -> int:
    """One reconciliation pass. Returns the number of jobs reconciled."""
    cursor = LogCursor(log_path, state_path)
    lines = cursor.read_new_lines()
    count = reconcile_tallies(aggregate(lines))
    cursor.save()
    if lines:
        log.info("accounting pass complete", extra={"lines": len(lines), "jobs": count})
    return count


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="printquota accounting daemon")
    parser.add_argument("--page-log", default=None, help="path to CUPS page_log")
    parser.add_argument("--state", default=None, help="path to the cursor state file")
    parser.add_argument("--interval", type=float, default=15.0, help="seconds between passes")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    global log
    log = setup_logging("accounting.daemon")
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    page_log = args.page_log or settings.get("printing.page_log", "/var/log/cups/page_log")
    state = args.state or os.environ.get("PRINTQUOTA_STATE", DEFAULT_STATE_PATH)

    if args.once:
        run_once(page_log, state)
        return 0

    stopping = {"flag": False}

    def _stop(signum, _frame):  # pragma: no cover - signal path
        log.info("shutting down", extra={"signal": signum})
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info("accounting daemon started", extra={"page_log": str(page_log), "interval": args.interval})
    warned_missing = False
    while not stopping["flag"]:
        if not Path(page_log).exists():
            if not warned_missing:
                log.warning(
                    "CUPS page_log does not exist; jobs stay charged at their pre-print page "
                    "count (this is normal for drivers that do not report pages)",
                    extra={"page_log": str(page_log)},
                )
                warned_missing = True
        elif warned_missing:
            log.info("page_log appeared; reconciling from it", extra={"page_log": str(page_log)})
            warned_missing = False
        try:
            run_once(page_log, state)
        except Exception:  # pragma: no cover - keep the daemon alive
            log.exception("accounting pass failed")
        slept = 0.0
        while slept < args.interval and not stopping["flag"]:
            time.sleep(min(0.5, args.interval - slept))
            slept += 0.5
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
