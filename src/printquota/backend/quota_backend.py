#!/usr/bin/env python3
"""CUPS wrapper backend: ``quota:<real-device-uri>``.

CUPS invokes a backend twice in its life:

* with **no arguments**, to discover devices -- we print one discovery line;
* with **6 or 7 arguments** for an actual job::

      argv[1] job-id  argv[2] user  argv[3] title
      argv[4] copies  argv[5] options  [argv[6] filename]

  With six arguments the job arrives on stdin.

The wrapper evaluates policy and quota, and on ALLOW execs the real backend
named by the device URI (``quota:socket://10.0.0.5:9100`` -> ``socket``)
with the same argument vector and ``DEVICE_URI`` rewritten to the real URI.

Exit codes are CUPS's own. A denied job returns ``CUPS_BACKEND_CANCEL`` so
the job is cancelled and the *queue stays enabled* -- returning FAILED would
stop the queue for everybody, which is the classic way a quota wrapper takes
a whole office offline.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from ..core.config import get_settings
from ..core.logging import get_logger, setup_logging
from ..db import session as db_session
from ..policies.engine import JobContext
from ..services.quota import authorize_job

# CUPS backend exit codes (cups/backend.h)
CUPS_BACKEND_OK = 0
CUPS_BACKEND_FAILED = 1
CUPS_BACKEND_AUTH_REQUIRED = 2
CUPS_BACKEND_HOLD = 3
CUPS_BACKEND_STOP = 4
CUPS_BACKEND_CANCEL = 5
CUPS_BACKEND_RETRY = 6

SCHEME = "quota"

_COLOR_OFF = ("gray", "grayscale", "monochrome", "black", "mono", "bw")
_OPTION_RE = re.compile(r"(?P<key>[A-Za-z0-9_.\-]+)=(?P<value>\"[^\"]*\"|'[^']*'|\S+)")

log = get_logger("backend")  # handlers attached in main()


def parse_options(raw: str) -> dict[str, str]:
    """Parse the CUPS options string (argv[5]) into a dict.

    Boolean options appear bare (``duplex``); ``key=value`` pairs may be
    quoted. Keys are lower-cased; values keep their case because some (like
    ``ColorModel``) are matched case-insensitively downstream anyway.
    """
    options: dict[str, str] = {}
    if not raw:
        return options
    for token in shlex.split(raw):
        match = _OPTION_RE.fullmatch(token)
        if match:
            value = match.group("value").strip("\"'")
            options[match.group("key").lower()] = value
        else:
            options[token.lower()] = "true"
    return options


def detect_color(options: dict[str, str]) -> bool:
    """Decide whether a job will consume colour toner."""
    mode = (options.get("print-color-mode") or "").lower()
    if mode:
        return mode not in ("monochrome", "auto-monochrome", "bi-level", "process-monochrome")
    model = (options.get("colormodel") or "").lower()
    if model:
        return not any(flag in model for flag in _COLOR_OFF)
    for key in ("color", "colour"):
        if key in options:
            return options[key].lower() not in ("false", "no", "off", "0")
    return False


def detect_duplex(options: dict[str, str]) -> bool:
    sides = (options.get("sides") or "").lower()
    if sides:
        return sides.startswith("two-sided")
    duplex = (options.get("duplex") or "").lower()
    if duplex:
        return duplex not in ("false", "no", "off", "none", "duplexnone", "0")
    return False


def detect_number_up(options: dict[str, str]) -> int:
    raw = options.get("number-up") or options.get("number_up") or "1"
    try:
        return max(int(raw), 1)
    except (TypeError, ValueError):
        return 1


def accepts_forced_sides(device_uri: str) -> bool:
    """Whether the real backend will act on a ``sides`` option added here.

    By the time this wrapper runs, CUPS has already rendered the job. Only
    the IPP backends send ``sides`` to the printer as a job attribute; the
    others (socket, lpd, usb) send the rendered bytes as they are.
    """
    try:
        scheme = split_device_uri(device_uri).split(":", 1)[0].lower()
    except ValueError:
        return False
    return scheme in ("ipp", "ipps")


def split_device_uri(device_uri: str) -> str:
    """Return the real device URI carried inside a ``quota:`` URI."""
    if not device_uri:
        raise ValueError("DEVICE_URI is not set")
    if not device_uri.startswith(f"{SCHEME}:"):
        raise ValueError(f"device URI {device_uri!r} is not a {SCHEME}: URI")
    remainder = device_uri[len(SCHEME) + 1 :]
    # Tolerate both quota:socket://host and quota://socket://host
    if remainder.startswith("//") and "://" in remainder[2:]:
        remainder = remainder[2:]
    if "://" not in remainder and ":" not in remainder:
        raise ValueError(f"device URI {device_uri!r} does not wrap a real device URI")
    return remainder


def real_backend_path(real_uri: str, backend_dir: str) -> Path:
    """Locate the executable for the wrapped scheme, with path validation."""
    scheme = real_uri.split(":", 1)[0].strip()
    if not scheme or not re.fullmatch(r"[a-z0-9][a-z0-9+.\-]*", scheme):
        raise ValueError(f"invalid backend scheme in {real_uri!r}")
    base = Path(backend_dir).resolve()
    candidate = (base / scheme).resolve()
    if candidate.parent != base:
        raise ValueError(f"backend path escapes {base}")
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ValueError(f"real backend {candidate} is missing or not executable")
    return candidate


def _stdin_to_tempfile() -> Optional[Path]:
    """Spool stdin to a temporary file so it can be counted and re-sent."""
    handle = tempfile.NamedTemporaryFile(prefix="printquota-", suffix=".spool", delete=False)
    try:
        shutil.copyfileobj(sys.stdin.buffer, handle)
    finally:
        handle.close()
    path = Path(handle.name)
    return path if path.stat().st_size else path


def discovery_line() -> str:
    return (
        f'network {SCHEME} "Unknown" "Print Quota wrapper backend" '
        f'"MFG:printquota;MDL:quota wrapper;"'
    )


def run(argv: list[str], environ: Optional[dict[str, str]] = None) -> int:
    """Backend entry point. Returns a CUPS backend exit code."""
    environ = dict(os.environ if environ is None else environ)
    settings = get_settings()

    if len(argv) == 1:
        print(discovery_line())
        return CUPS_BACKEND_OK
    if len(argv) not in (6, 7):
        sys.stderr.write(f"ERROR: usage: {argv[0]} job-id user title copies options [file]\n")
        return CUPS_BACKEND_FAILED

    job_id_raw, username, title, copies_raw, option_string = argv[1:6]
    file_arg = argv[6] if len(argv) == 7 else None

    try:
        cups_job_id = int(job_id_raw)
    except (TypeError, ValueError):
        cups_job_id = None
    try:
        copies = max(int(copies_raw), 1)
    except (TypeError, ValueError):
        copies = 1

    printer = environ.get("PRINTER") or "unknown"
    options = parse_options(option_string)
    is_color = detect_color(options)
    is_duplex = detect_duplex(options)
    number_up = detect_number_up(options)

    temp_spool: Optional[Path] = None
    spool_path: Optional[Path] = Path(file_arg) if file_arg else None
    if spool_path is None:
        temp_spool = _stdin_to_tempfile()
        spool_path = temp_spool

    try:
        from ..accounting.pages import estimate_spooled_job

        # Count the documents the client submitted (still in the CUPS spool),
        # not the driver output on stdin, which usually cannot be counted.
        estimate = estimate_spooled_job(
            str(settings.get("printing.spool_dir", "/var/spool/cups") or ""),
            cups_job_id,
            spool_path,
            copies=copies,
            number_up=number_up,
            timeout=int(settings.get("printing.estimator_timeout", 15)),
        )
        pages = estimate.total_pages
        method = estimate.method
        copies = estimate.copies
        if estimate.two_sided is not None:
            # Printer-ready data (e.g. from a Ricoh DDST driver) prints the way
            # it says, whatever sides CUPS's queue default put in the options.
            is_duplex = estimate.two_sided
    except Exception as exc:  # estimation must never crash the queue
        log.warning("page estimation failed", extra={"error": str(exc), "job": cups_job_id})
        pages = copies
        method = "error"

    ctx = JobContext(
        username=username,
        printer=printer,
        estimated_pages=pages,
        copies=copies,
        is_color=is_color,
        is_duplex=is_duplex,
        filetype=(spool_path.suffix.lstrip(".") if spool_path else None),
        title=title,
        can_force_sides=accepts_forced_sides(environ.get("DEVICE_URI", "")),
    )

    try:
        with db_session.session_scope() as session:
            decision, _job = authorize_job(session, ctx, cups_job_id=cups_job_id)
        allowed = decision.allowed
        reason = decision.reason
        forced = dict(decision.forced_options)
    except Exception as exc:
        # Fail closed on an unreachable datastore: holding the job is safer
        # than printing an unaccounted one, and the job is not lost.
        log.error("quota check failed", extra={"error": str(exc), "job": cups_job_id})
        sys.stderr.write("ERROR: print quota service unavailable; job held\n")
        _cleanup(temp_spool)
        return CUPS_BACKEND_HOLD

    log.info(
        "decision",
        extra={
            "job": cups_job_id,
            "user": username,
            "printer": printer,
            "pages": pages,
            "method": method,
            "color": is_color,
            "duplex": is_duplex,
            "allowed": allowed,
            "reason": reason,
        },
    )

    if not allowed:
        sys.stderr.write(f"ERROR: print job denied: {reason}\n")
        sys.stderr.write(f"STATE: +printquota-job-denied\n")
        _cleanup(temp_spool)
        return CUPS_BACKEND_CANCEL

    try:
        real_uri = split_device_uri(environ.get("DEVICE_URI", ""))
        backend = real_backend_path(
            real_uri, str(settings.get("printing.real_backend_dir", "/usr/lib/cups/backend"))
        )
    except ValueError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        _cleanup(temp_spool)
        return CUPS_BACKEND_STOP

    child_options = option_string
    if forced:
        child_options = " ".join(
            [option_string] + [f"{key}={value}" for key, value in forced.items()]
        ).strip()

    child_argv = [
        str(backend),
        job_id_raw,
        username,
        title,
        copies_raw,
        child_options,
        str(spool_path),
    ]
    child_env = dict(environ)
    child_env["DEVICE_URI"] = real_uri

    try:
        completed = subprocess.run(child_argv, env=child_env, check=False)
        return completed.returncode
    except OSError as exc:
        sys.stderr.write(f"ERROR: could not run real backend {backend}: {exc}\n")
        return CUPS_BACKEND_FAILED
    finally:
        _cleanup(temp_spool)


def _cleanup(path: Optional[Path]) -> None:
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass


def main() -> int:
    global log
    log = setup_logging("backend")
    try:
        return run(sys.argv)
    except Exception as exc:  # pragma: no cover - last-resort guard
        sys.stderr.write(f"ERROR: print quota backend crashed: {exc}\n")
        log.exception("backend crashed")
        return CUPS_BACKEND_HOLD


if __name__ == "__main__":
    sys.exit(main())
