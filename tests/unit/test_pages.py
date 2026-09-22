"""Page estimation across the spool formats CUPS actually hands us."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from printquota.accounting.pages import (
    DEFAULT_PAGES,
    PageEstimate,
    detect_format,
    estimate_file_pages,
    estimate_job,
)
from printquota.core.exceptions import EstimationError


def _write_pdf(path, pages: int) -> None:
    """Build a tiny valid multi-page PDF without external tooling."""
    objects = []
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objects.append(f"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>")
    for _ in range(pages):
        objects.append("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>")
    body = "%PDF-1.4\n"
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{index} 0 obj\n{obj}\nendobj\n"
    xref_pos = len(body)
    body += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    for offset in offsets:
        body += f"{offset:010d} 00000 n \n"
    body += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    path.write_bytes(body.encode("latin-1"))


@pytest.mark.skipif(shutil.which("pdfinfo") is None, reason="poppler-utils not installed")
def test_pdf_page_count_comes_from_pdfinfo(tmp_path):
    path = tmp_path / "doc.pdf"
    _write_pdf(path, 7)
    assert detect_format(path) == "pdf"
    pages, method = estimate_file_pages(path)
    assert (pages, method) == (7, "pdf")


def test_postscript_uses_the_trailer_page_count(tmp_path):
    path = tmp_path / "doc.ps"
    path.write_bytes(b"%!PS-Adobe-3.0\n%%Pages: (atend)\n%%Page: 1 1\n%%Page: 2 2\n%%Pages: 2\n")
    assert detect_format(path) == "postscript"
    assert estimate_file_pages(path)[0] == 2


def test_plain_text_is_estimated_by_line_count(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("line\n" * 125)
    assert estimate_file_pages(path)[0] == 3


def test_pcl_counts_form_feeds(tmp_path):
    path = tmp_path / "doc.pcl"
    path.write_bytes(b"\x1bE" + b"text\x0c" * 4)
    assert detect_format(path) == "pcl"
    assert estimate_file_pages(path)[0] == 4


def test_unparseable_payload_falls_back_rather_than_failing(tmp_path):
    path = tmp_path / "doc.bin"
    path.write_bytes(b"\x00\x01\x02\x03")
    pages, method = estimate_file_pages(path)
    assert pages == DEFAULT_PAGES
    assert method.endswith("fallback")


def test_empty_file_is_charged_the_default(tmp_path):
    path = tmp_path / "empty.prn"
    path.touch()
    assert estimate_file_pages(path) == (DEFAULT_PAGES, "empty")


def test_missing_file_raises(tmp_path):
    with pytest.raises(EstimationError):
        estimate_file_pages(tmp_path / "gone.pdf")


def test_copies_and_number_up_are_applied():
    assert PageEstimate(pages_per_copy=10, copies=3).total_pages == 30
    assert PageEstimate(pages_per_copy=10, copies=1, number_up=2).total_pages == 5
    assert PageEstimate(pages_per_copy=9, copies=2, number_up=2).total_pages == 10


def test_estimate_job_combines_parsing_and_multipliers(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("line\n" * 60)
    estimate = estimate_job(path, copies=2)
    assert estimate.pages_per_copy == 1 and estimate.total_pages == 2
