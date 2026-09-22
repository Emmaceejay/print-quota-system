"""Pre-flight page estimation.

CUPS hands the backend a spool file (or the job on stdin). We need a page
count *before* the job reaches the printer, and the authoritative count only
exists afterwards in ``page_log``. This module produces the estimate; the
accounting daemon reconciles it later.

Security note: the spool path comes from CUPS, but it is still passed to an
external binary, so it is resolved and validated and every subprocess call
is argument-list based (never a shell string) with a timeout.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..core.exceptions import EstimationError
from ..core.logging import get_logger

log = get_logger("accounting.pages")

_PDF_PAGES_RE = re.compile(rb"^Pages:\s+(\d+)", re.MULTILINE)
_PS_PAGES_RE = re.compile(rb"%%Pages:\s*(\d+)")
_PCL_FORMFEED = b"\x0c"
#: Conservative fallback when nothing can be parsed.
DEFAULT_PAGES = 1
TEXT_LINES_PER_PAGE = 60


@dataclass(frozen=True)
class PageEstimate:
    """Result of estimating a job."""

    pages_per_copy: int
    copies: int
    number_up: int = 1
    method: str = "unknown"

    @property
    def total_pages(self) -> int:
        """Sides that will be imaged, accounting for N-up and copies."""
        per_copy = max(self.pages_per_copy, 1)
        nup = max(self.number_up, 1)
        sides_per_copy = -(-per_copy // nup)
        return sides_per_copy * max(self.copies, 1)


def detect_format(path: Path) -> str:
    """Sniff the spool file format from its magic bytes."""
    try:
        with path.open("rb") as handle:
            head = handle.read(1024)
    except OSError as exc:
        raise EstimationError(f"cannot read spool file {path}: {exc}") from exc
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"%!PS") or b"%!PS-Adobe" in head[:256]:
        return "postscript"
    if head.startswith(b"\x1b%-12345X") or head.startswith(b"\x1bE"):
        return "pcl"
    if head.startswith(b"\x1b"):
        return "escp"
    if b"\x00" in head:
        return "binary"
    printable = sum(1 for byte in head if 32 <= byte < 127 or byte in (9, 10, 13))
    if head and printable / len(head) < 0.9:
        return "binary"
    return "text"


def _pdfinfo_pages(path: Path, timeout: int) -> Optional[int]:
    binary = shutil.which("pdfinfo")
    if not binary:
        log.warning("pdfinfo not found; install poppler-utils for accurate estimates")
        return None
    try:
        proc = subprocess.run(
            [binary, str(path)],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("pdfinfo timed out", extra={"path": str(path)})
        return None
    if proc.returncode != 0:
        log.warning(
            "pdfinfo failed",
            extra={"path": str(path), "rc": proc.returncode, "err": proc.stderr[:200].decode(errors="replace")},
        )
        return None
    match = _PDF_PAGES_RE.search(proc.stdout)
    return int(match.group(1)) if match else None


def _postscript_pages(path: Path) -> Optional[int]:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    matches = _PS_PAGES_RE.findall(data)
    for raw in reversed(matches):  # trailer wins over a placeholder header
        value = int(raw)
        if value > 0:
            return value
    pages = data.count(b"%%Page:")
    return pages or None


def _pcl_pages(path: Path) -> Optional[int]:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    pages = data.count(_PCL_FORMFEED)
    return pages or None


def _text_pages(path: Path) -> Optional[int]:
    try:
        with path.open("rb") as handle:
            lines = sum(1 for _ in handle)
    except OSError:
        return None
    if lines == 0:
        return None
    return max(1, -(-lines // TEXT_LINES_PER_PAGE))


def estimate_file_pages(path: str | Path, timeout: int = 15) -> tuple[int, str]:
    """Return ``(pages_per_copy, method)`` for a spool file.

    Never raises for a merely unparseable file -- an unknown payload falls
    back to :data:`DEFAULT_PAGES` so that a weird job is charged something
    rather than slipping through free; the accounting daemon corrects it
    from ``page_log`` once the job completes.
    """
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise EstimationError(f"spool file does not exist: {resolved}")
    if resolved.stat().st_size == 0:
        return DEFAULT_PAGES, "empty"

    fmt = detect_format(resolved)
    pages: Optional[int] = None
    if fmt == "pdf":
        pages = _pdfinfo_pages(resolved, timeout)
    elif fmt == "postscript":
        pages = _postscript_pages(resolved)
    elif fmt == "pcl":
        pages = _pcl_pages(resolved)
    elif fmt == "text":
        pages = _text_pages(resolved)

    if pages and pages > 0:
        return pages, fmt
    return DEFAULT_PAGES, f"{fmt}:fallback"


def estimate_job(
    path: str | Path,
    copies: int = 1,
    number_up: int = 1,
    timeout: int = 15,
) -> PageEstimate:
    """Estimate the total sides a job will image."""
    pages, method = estimate_file_pages(path, timeout=timeout)
    return PageEstimate(
        pages_per_copy=pages,
        copies=max(int(copies or 1), 1),
        number_up=max(int(number_up or 1), 1),
        method=method,
    )
