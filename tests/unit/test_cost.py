"""Cost arithmetic."""

from __future__ import annotations

import pytest

from printquota.accounting.cost import PrinterRates, job_cost, per_page_rate, sheets_used


def test_mono_and_colour_rates():
    rates = PrinterRates(cost_per_page_mono=2.0, cost_per_page_color=10.0)
    assert per_page_rate(rates, is_color=False, is_duplex=False) == 2.0
    assert per_page_rate(rates, is_color=True, is_duplex=False) == 10.0


def test_duplex_discount_applies_per_page():
    rates = PrinterRates(cost_per_page_mono=2.0, duplex_discount=0.5)
    assert per_page_rate(rates, is_color=False, is_duplex=True) == 1.0
    assert job_cost(10, rates, is_duplex=True) == 10.0
    assert job_cost(10, rates, is_duplex=False) == 20.0


def test_zero_and_negative_pages_cost_nothing():
    rates = PrinterRates(cost_per_page_mono=2.0)
    assert job_cost(0, rates) == 0.0
    assert job_cost(-5, rates) == 0.0


@pytest.mark.parametrize(
    "pages,duplex,nup,expected",
    [(1, False, 1, 1), (10, False, 1, 10), (10, True, 1, 5), (9, True, 1, 5), (8, False, 2, 4), (8, True, 2, 2)],
)
def test_sheet_count(pages, duplex, nup, expected):
    assert sheets_used(pages, is_duplex=duplex, number_up=nup) == expected


def test_rates_fall_back_to_configured_defaults():
    class FakePrinter:
        cost_per_page_mono = 0.0
        cost_per_page_color = 0.0
        duplex_discount = 0.0

    rates = PrinterRates.from_printer(
        FakePrinter(), {"default_cost_per_page_mono": 1.5, "default_cost_per_page_color": 7.0}
    )
    assert rates.cost_per_page_mono == 1.5
    assert rates.cost_per_page_color == 7.0
