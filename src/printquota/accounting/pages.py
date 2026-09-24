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
    if head.startswith(b"UNIRAST\x00"):
        return "urf"
    if head.startswith((b"RaS2", b"RaS3", b"2SaR", b"3SaR")):
        return "pwg-raster"
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


def _urf_pages(path: Path) -> Optional[int]:
    """Apple raster (AirPrint): the page count is a uint32 right after the magic."""
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
    except OSError:
        return None
    if len(header) < 12:
        return None
    return int.from_bytes(header[8:12], "big") or None


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
    elif fmt == "urf":
        pages = _urf_pages(resolved)

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


def _reliable(method: str) -> bool:
    return not (method.endswith(":fallback") or method in ("empty", "error"))


def estimate_spooled_job(
    spool_dir: Optional[str],
    job_id: Optional[int],
    received: Optional[str | Path],
    copies: int = 1,
    number_up: int = 1,
    timeout: int = 15,
) -> PageEstimate:
    """Estimate a job from the documents the client submitted.

    The data a backend receives (``received``) has usually been converted to
    the printer's language by the driver, which cannot be counted. So this
    counts the original documents in the CUPS spool first, applying the
    job's own ``copies``, ``number-up`` and ``page-ranges``, and falls back
    to ``received`` only when the originals are missing or unreadable.
    """
    from . import cups_spool

    attrs: dict = {}
    per_copy = 0
    methods: list[str] = []
    if spool_dir and job_id is not None:
        attrs = cups_spool.read_job_attributes(spool_dir, job_id)
        for document in cups_spool.document_paths(spool_dir, job_id):
            readable, temp = cups_spool.readable_copy(document)
            try:
                pages, method = estimate_file_pages(readable, timeout=timeout)
            except EstimationError:
                pages, method = DEFAULT_PAGES, "error"
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)
            per_copy += cups_spool.pages_in_ranges(pages, attrs.get("page-ranges"))
            methods.append(method)

    job_copies = max(int(attrs.get("copies") or copies or 1), 1)
    job_nup = max(int(attrs.get("number-up") or number_up or 1), 1)

    if methods and all(_reliable(m) for m in methods):
        return PageEstimate(per_copy, job_copies, job_nup, method="spool:" + "+".join(methods))

    fallback: Optional[PageEstimate] = None
    if received is not None:
        try:
            fallback = estimate_job(received, copies=job_copies, number_up=job_nup, timeout=timeout)
        except EstimationError:
            fallback = None
    if fallback is not None and _reliable(fallback.method):
        return fallback
    # Neither source could be counted: charge the larger guess rather than
    # letting an uncountable job through as a single page.
    candidates = [fallback] if fallback is not None else []
    if methods:
        candidates.append(PageEstimate(per_copy, job_copies, job_nup, method="spool:" + "+".join(methods)))
    if not candidates:
        return PageEstimate(DEFAULT_PAGES, job_copies, job_nup, method="unknown:fallback")
    return max(candidates, key=lambda e: e.total_pages)
