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
    #: Two-sided setting found inside the print data itself (``None`` when
    #: the format doesn't say). A printer-ready job is printed the way its
    #: data says, whatever the queue's own default is.
    two_sided: Optional[bool] = None

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
    if head.startswith(_GDI_JOB) and len(head) >= _GDI_HEADER_LEN:
        return "ricoh-gdi"
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


#: Ricoh GDI ("DDST") jobs, made by Ricoh's Windows DDST drivers (e.g. MP
#: 2014AD DDST). A job is a series of tagged blocks: GDIJ (job header), GJET,
#: then per page GDIP (64 bytes) followed by GPHE and the image bands (GDIB),
#: and JIDG at the end. In the job header, bytes 10-11 hold the copies the
#: printer makes and byte 16 is 0 for one-sided, non-zero for two-sided.
_GDI_JOB = b"GDIJ"
_GDI_PAGE = b"GDIP"
_GDI_PAGE_HEADER = b"GPHE"
_GDI_PAGE_LEN = 64
_GDI_HEADER_LEN = 24


@dataclass(frozen=True)
class GdiJob:
    pages: int
    copies: int
    two_sided: bool


def _ricoh_gdi_job(path: Path) -> Optional[GdiJob]:
    """Pages, copies and sides of a Ricoh GDI job, or None if it isn't one."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(_GDI_JOB) or len(data) < _GDI_HEADER_LEN:
        return None
    pages = 0
    start = data.find(_GDI_PAGE)
    while start != -1:
        # A real page block is followed by its page header; the tag bytes
        # occurring by chance inside image data are not.
        tail = start + _GDI_PAGE_LEN
        if data[tail:tail + len(_GDI_PAGE_HEADER)] == _GDI_PAGE_HEADER:
            pages += 1
        start = data.find(_GDI_PAGE, start + 1)
    if pages == 0:
        return None
    copies = max(int.from_bytes(data[10:12], "big"), 1)
    return GdiJob(pages=pages, copies=copies, two_sided=data[16] != 0)


def _text_pages(path: Path) -> Optional[int]:
    try:
        with path.open("rb") as handle:
            lines = sum(1 for _ in handle)
    except OSError:
        return None
    if lines == 0:
        return None
    return max(1, -(-lines // TEXT_LINES_PER_PAGE))


@dataclass(frozen=True)
class FileCount:
    """What one spool file contains."""

    pages: int
    method: str
    #: Copies the printer makes from this data (1 unless the data says).
    copies: int = 1
    two_sided: Optional[bool] = None


def inspect_file(path: str | Path, timeout: int = 15) -> FileCount:
    """Pages per copy of a spool file, plus copies and sides when the data says."""
    resolved = Path(path).resolve()
    if resolved.is_file() and detect_format(resolved) == "ricoh-gdi":
        job = _ricoh_gdi_job(resolved)
        if job is not None:
            return FileCount(job.pages, "ricoh-gdi", copies=job.copies, two_sided=job.two_sided)
    pages, method = estimate_file_pages(resolved, timeout=timeout)
    return FileCount(pages, method)


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
    elif fmt == "ricoh-gdi":
        job = _ricoh_gdi_job(resolved)
        pages = job.pages * job.copies if job else None

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
    found = inspect_file(path, timeout=timeout)
    return PageEstimate(
        pages_per_copy=found.pages,
        copies=max(int(copies or 1), 1) * found.copies,
        number_up=max(int(number_up or 1), 1),
        method=found.method,
        two_sided=found.two_sided,
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
    found: list[FileCount] = []
    if spool_dir and job_id is not None:
        attrs = cups_spool.read_job_attributes(spool_dir, job_id)
        for document in cups_spool.document_paths(spool_dir, job_id):
            readable, temp = cups_spool.readable_copy(document)
            try:
                count = inspect_file(readable, timeout=timeout)
            except EstimationError:
                count = FileCount(DEFAULT_PAGES, "error")
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)
            found.append(count)
            methods.append(count.method)

    job_copies = max(int(attrs.get("copies") or copies or 1), 1)
    job_nup = max(int(attrs.get("number-up") or number_up or 1), 1)
    # Copies made by the printer from the data itself (e.g. a Ricoh GDI job
    # for 2 copies). With one document they are true copies; across several
    # documents they are folded into the page count.
    if len(found) == 1:
        job_copies *= found[0].copies
        per_copy = cups_spool.pages_in_ranges(found[0].pages, attrs.get("page-ranges"))
    else:
        per_copy = sum(
            cups_spool.pages_in_ranges(c.pages, attrs.get("page-ranges")) * c.copies for c in found
        )
    sides = {c.two_sided for c in found if c.two_sided is not None}
    two_sided = sides.pop() if len(sides) == 1 else None

    if methods and all(_reliable(m) for m in methods):
        return PageEstimate(
            per_copy, job_copies, job_nup, method="spool:" + "+".join(methods), two_sided=two_sided
        )

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
        candidates.append(PageEstimate(
            per_copy, job_copies, job_nup, method="spool:" + "+".join(methods), two_sided=two_sided
        ))
    if not candidates:
        return PageEstimate(DEFAULT_PAGES, job_copies, job_nup, method="unknown:fallback")
    return max(candidates, key=lambda e: e.total_pages)
