"""Read a job's original documents and attributes from the CUPS spool.

When a queue has a driver (a PPD), CUPS converts the document into the
printer's own language *before* the backend runs, so the data the backend
receives on stdin is PCL XL, raster or similar -- formats whose pages cannot
be counted reliably. The document the client actually submitted is still in
the spool directory (``RequestRoot``, ``/var/spool/cups`` by default)::

    d<job:05d>-<doc:03d>   each submitted document, unchanged
    c<job:05d>             the job's IPP attributes (copies, page-ranges, ...)

The backend runs as root, so it can read both. Everything here is
best-effort: a missing or unreadable file returns nothing and the caller
falls back to estimating from the data it was given.
"""

from __future__ import annotations

import gzip
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from ..core.logging import get_logger

log = get_logger("accounting.cups_spool")

#: IPP value tags this module understands.
_TAG_END = 0x03
_TAG_INTEGER = 0x21
_TAG_RANGE = 0x33
#: Guard against a corrupt control file making us read garbage for ever.
_MAX_CONTROL_BYTES = 4 * 1024 * 1024


def document_paths(spool_dir: str | Path, job_id: int) -> list[Path]:
    """The job's submitted documents, in order (empty if none can be found)."""
    base = Path(spool_dir)
    try:
        return sorted(p for p in base.glob(f"d{job_id:05d}-[0-9][0-9][0-9]") if p.is_file())
    except OSError:
        return []


def read_job_attributes(spool_dir: str | Path, job_id: int) -> dict:
    """``copies``, ``number-up`` and ``page-ranges`` from the job control file.

    The control file is a serialised IPP message: an 8-byte header followed
    by ``tag, name-length, name, value-length, value`` records, where a record
    with an empty name is an additional value of the previous attribute.
    """
    path = Path(spool_dir) / f"c{job_id:05d}"
    try:
        data = path.read_bytes()[:_MAX_CONTROL_BYTES]
    except OSError:
        return {}
    attrs: dict = {}
    pos = 8
    name = b""
    try:
        while pos < len(data):
            tag = data[pos]
            pos += 1
            if tag == _TAG_END:
                break
            if tag < 0x10:  # delimiter: start of the next attribute group
                continue
            name_len = int.from_bytes(data[pos:pos + 2], "big")
            pos += 2
            if name_len:
                name = data[pos:pos + name_len]
            pos += name_len
            value_len = int.from_bytes(data[pos:pos + 2], "big")
            pos += 2
            value = data[pos:pos + value_len]
            pos += value_len
            if tag == _TAG_INTEGER and value_len == 4 and name in (b"copies", b"number-up"):
                attrs[name.decode()] = int.from_bytes(value, "big", signed=True)
            elif tag == _TAG_RANGE and value_len == 8 and name == b"page-ranges":
                low = int.from_bytes(value[:4], "big", signed=True)
                high = int.from_bytes(value[4:], "big", signed=True)
                attrs.setdefault("page-ranges", []).append((low, high))
    except (IndexError, ValueError):  # pragma: no cover - truncated file
        log.warning("could not parse job control file", extra={"path": str(path)})
    return attrs


def pages_in_ranges(total: int, ranges: Optional[list[tuple[int, int]]]) -> int:
    """How many of ``total`` pages fall inside the requested page ranges."""
    if not ranges:
        return total
    selected: set[int] = set()
    for low, high in ranges:
        low, high = max(low, 1), min(high, total)
        if low <= high:
            selected.update(range(low, high + 1))
    return len(selected) or total


def readable_copy(path: Path) -> tuple[Path, Optional[Path]]:
    """Return a path whose contents are uncompressed.

    Clients may send a document gzip-compressed (``compression=gzip``); CUPS
    stores it as received. Returns ``(path_to_read, temp_file_to_delete)``.
    """
    try:
        with path.open("rb") as handle:
            if handle.read(2) != b"\x1f\x8b":
                return path, None
    except OSError:
        return path, None
    temp = tempfile.NamedTemporaryFile(prefix="printquota-doc-", delete=False)
    try:
        with gzip.open(path, "rb") as source:
            shutil.copyfileobj(source, temp)
    except (OSError, EOFError):
        temp.close()
        Path(temp.name).unlink(missing_ok=True)
        return path, None
    temp.close()
    return Path(temp.name), Path(temp.name)
