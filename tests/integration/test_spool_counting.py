"""Counting pages from the documents the client submitted.

On a queue with a driver, CUPS converts the job to the printer's language
before the backend runs, so the backend's stdin cannot be counted. These
tests reproduce that: stdin carries uncountable printer data while the
original document sits in the spool directory, as it does under CUPS.
"""

from __future__ import annotations

import gzip
import os

from printquota.accounting import cups_spool
from printquota.accounting.pages import estimate_spooled_job
from printquota.backend import quota_backend as qb
from printquota.db import session as db_session
from printquota.db.models import PrintJob, User

#: What a driver typically hands the backend: binary printer language with
#: no page markers we can count (PCL XL here).
DRIVER_OUTPUT = b"\x1b%-12345X@PJL ENTER LANGUAGE=PCLXL\r\n) HP-PCL XL;3;0\r\n" + bytes(b for b in range(256) if b != 0x0C) * 20


def windows_postscript(pages: int) -> bytes:
    """Shaped like the output of Windows' Microsoft PS Class Driver."""
    body = ["%!PS-Adobe-3.0", "%%Creator: PScript5.dll Version 5.2.2", "%%Pages: (atend)", "%%EndComments"]
    for page in range(1, pages + 1):
        body += [f"%%Page: {page} {page}", "showpage"]
    body += ["%%Trailer", f"%%Pages: {pages}", "%%EOF"]
    return ("\r\n".join(body) + "\r\n").encode()


def ipp_control_file(copies: int = 1, number_up: int | None = None,
                     page_ranges: list[tuple[int, int]] | None = None) -> bytes:
    """A minimal serialised IPP message, as CUPS writes c<job> files."""
    def attr(tag: int, name: bytes, value: bytes) -> bytes:
        return bytes([tag]) + len(name).to_bytes(2, "big") + name + len(value).to_bytes(2, "big") + value

    out = b"\x02\x00\x00\x02\x00\x00\x00\x01"  # version 2.0, Print-Job, request 1
    out += b"\x01" + attr(0x47, b"attributes-charset", b"utf-8")
    out += b"\x02" + attr(0x42, b"job-name", b"Quarterly report")
    out += attr(0x21, b"copies", copies.to_bytes(4, "big"))
    if number_up:
        out += attr(0x21, b"number-up", number_up.to_bytes(4, "big"))
    for index, (low, high) in enumerate(page_ranges or []):
        out += attr(0x33, b"page-ranges" if index == 0 else b"", low.to_bytes(4, "big") + high.to_bytes(4, "big"))
    return out + b"\x03"


def put_job(env, job_id: int, documents: list[bytes], **attrs) -> None:
    for index, data in enumerate(documents, start=1):
        (env["spool_dir"] / f"d{job_id:05d}-{index:03d}").write_bytes(data)
    (env["spool_dir"] / f"c{job_id:05d}").write_bytes(ipp_control_file(**attrs))


def run_through_driver(env, monkeypatch, job_id: int = 41, user: str = "ceejay", copies: str = "1") -> int:
    """Invoke the backend the way CUPS does for a driver queue: 6 args, data on stdin."""
    received = env["tmp_path"] / "driver-output.bin"
    received.write_bytes(DRIVER_OUTPUT)
    monkeypatch.setattr(qb, "_stdin_to_tempfile", lambda: received)
    argv = ["quota", str(job_id), user, "Quarterly report", copies, ""]
    environ = {**os.environ, "PRINTER": "hp-mono", "DEVICE_URI": "quota:socket://10.0.0.5:9100"}
    return qb.run(argv, environ)


# ---------------------------------------------------------------- the reported bug
def test_a_4_page_job_with_3_pages_left_is_refused_before_printing(seeded, monkeypatch, capsys):
    with db_session.session_scope() as session:
        user = session.get(User, "ceejay")
        user.quota_limit, user.pages_used = 3, 0
    put_job(seeded, 41, [windows_postscript(4)])

    rc = run_through_driver(seeded, monkeypatch)

    assert rc == qb.CUPS_BACKEND_CANCEL
    assert "4 page(s) requested" in capsys.readouterr().err
    with db_session.session_scope() as session:
        assert session.get(User, "ceejay").pages_used == 0
        assert session.query(PrintJob).one().status == PrintJob.STATUS_DENIED


def test_an_allowed_job_is_charged_its_real_page_count(seeded, monkeypatch):
    put_job(seeded, 42, [windows_postscript(4)])
    run_through_driver(seeded, monkeypatch, job_id=42)  # handing off to the printer is not under test here
    with db_session.session_scope() as session:
        job = session.query(PrintJob).one()
        assert job.estimated_pages == 4
        assert session.get(User, "ceejay").pages_used == 4


def test_copies_come_from_the_job_not_the_driver(seeded, monkeypatch):
    put_job(seeded, 43, [windows_postscript(4)], copies=3)
    run_through_driver(seeded, monkeypatch, job_id=43, copies="1")
    with db_session.session_scope() as session:
        assert session.query(PrintJob).one().estimated_pages == 12


def test_without_spool_files_the_received_data_is_still_used(seeded, monkeypatch):
    received = seeded["tmp_path"] / "plain.txt"
    received.write_text("line\n" * 130)  # 3 pages of text
    monkeypatch.setattr(qb, "_stdin_to_tempfile", lambda: received)
    qb.run(["quota", "44", "ceejay", "notes", "1", ""],
           {**os.environ, "PRINTER": "hp-mono", "DEVICE_URI": "quota:socket://10.0.0.5:9100"})
    with db_session.session_scope() as session:
        assert session.query(PrintJob).one().estimated_pages == 3


# ------------------------------------------------------------------ estimator
def test_page_ranges_limit_the_count(seeded):
    put_job(seeded, 50, [windows_postscript(10)], page_ranges=[(2, 4), (9, 20)])
    estimate = estimate_spooled_job(str(seeded["spool_dir"]), 50, None)
    assert estimate.total_pages == 5  # pages 2-4 and 9-10


def test_multiple_documents_and_number_up(seeded):
    put_job(seeded, 51, [windows_postscript(3), windows_postscript(5)], copies=2, number_up=2)
    estimate = estimate_spooled_job(str(seeded["spool_dir"]), 51, None)
    assert estimate.total_pages == 8  # 8 pages, 2-up -> 4 sides, x2 copies
    assert estimate.method.startswith("spool:")


def test_gzip_compressed_documents_are_read(seeded):
    put_job(seeded, 52, [gzip.compress(windows_postscript(6))])
    assert estimate_spooled_job(str(seeded["spool_dir"]), 52, None).total_pages == 6


def test_uncountable_everything_charges_the_larger_guess(seeded):
    (seeded["spool_dir"] / "d00053-001").write_bytes(bytes(range(256)) * 10)
    received = seeded["tmp_path"] / "raw.bin"
    received.write_bytes(DRIVER_OUTPUT)
    estimate = estimate_spooled_job(str(seeded["spool_dir"]), 53, received, copies=2)
    assert estimate.total_pages == 2 and estimate.method.endswith("fallback")


def test_control_file_parsing_tolerates_junk(seeded):
    (seeded["spool_dir"] / "c00060").write_bytes(b"\x00" * 3)
    assert cups_spool.read_job_attributes(seeded["spool_dir"], 60) == {}
    assert cups_spool.read_job_attributes(seeded["spool_dir"], 61) == {}
    assert cups_spool.document_paths(seeded["spool_dir"], 61) == []
