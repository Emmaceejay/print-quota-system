"""Counting jobs from Ricoh's Windows DDST drivers (Ricoh GDI format)."""

from __future__ import annotations

from pathlib import Path

import pytest

from printquota.accounting.pages import detect_format, estimate_file_pages, estimate_job, inspect_file

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ricoh-gdi"


@pytest.mark.parametrize("name,pages,copies,two_sided", [
    ("simplex-1", 1, 1, False),
    ("simplex-3", 3, 1, False),
    ("simplex-2-copies2", 2, 2, False),
    ("duplex-long-3", 3, 1, True),
    ("duplex-short-3", 3, 1, True),
])
def test_pages_copies_and_sides_are_read_from_the_job(name, pages, copies, two_sided):
    path = FIXTURES / f"{name}.prn"
    assert detect_format(path) == "ricoh-gdi"
    found = inspect_file(path)
    assert (found.pages, found.copies, found.two_sided, found.method) == (pages, copies, two_sided, "ricoh-gdi")


def test_the_plain_page_count_includes_the_printers_copies():
    assert estimate_file_pages(FIXTURES / "simplex-2-copies2.prn") == (4, "ricoh-gdi")


def test_an_estimate_carries_copies_and_sides():
    estimate = estimate_job(FIXTURES / "duplex-long-3.prn", copies=2)
    assert estimate.pages_per_copy == 3 and estimate.copies == 2 and estimate.two_sided is True
    assert estimate.total_pages == 6


def test_page_tags_inside_image_data_are_not_counted(tmp_path):
    data = (FIXTURES / "simplex-1.prn").read_bytes()
    noisy = data[:-4] + b"GDIP" + b"\x00" * 80 + data[-4:]  # stray tag, no page header after it
    path = tmp_path / "noisy.prn"
    path.write_bytes(noisy)
    assert inspect_file(path).pages == 1


def test_a_truncated_job_falls_back_rather_than_crashing(tmp_path):
    path = tmp_path / "cut.prn"
    path.write_bytes((FIXTURES / "simplex-3.prn").read_bytes()[:200])  # header only, no pages
    pages, method = estimate_file_pages(path)
    assert pages == 1 and method.endswith("fallback")
