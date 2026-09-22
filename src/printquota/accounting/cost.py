"""Cost arithmetic.

Pure functions: no ORM, no configuration lookups at call sites. Callers pass
a :class:`PrinterRates` built from the printer row (falling back to the
configured defaults when the printer has no explicit cost model).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PrinterRates:
    """Per-page money rates for one queue."""

    cost_per_page_mono: float = 0.0
    cost_per_page_color: float = 0.0
    #: Fraction taken off each page's cost when the job is duplex; ``0.5``
    #: means a duplex page costs half, because two sides share one sheet.
    duplex_discount: float = 0.0

    @classmethod
    def from_printer(cls, printer, defaults: Optional[dict] = None) -> "PrinterRates":
        defaults = defaults or {}
        if printer is None:
            return cls(
                cost_per_page_mono=float(defaults.get("default_cost_per_page_mono", 0.0)),
                cost_per_page_color=float(defaults.get("default_cost_per_page_color", 0.0)),
                duplex_discount=float(defaults.get("default_duplex_discount", 0.0)),
            )
        mono = printer.cost_per_page_mono
        color = printer.cost_per_page_color
        return cls(
            cost_per_page_mono=float(
                mono if mono else defaults.get("default_cost_per_page_mono", 0.0)
            ),
            cost_per_page_color=float(
                color if color else defaults.get("default_cost_per_page_color", 0.0)
            ),
            duplex_discount=float(printer.duplex_discount or 0.0),
        )


def per_page_rate(rates: PrinterRates, *, is_color: bool, is_duplex: bool) -> float:
    """Effective money cost of a single printed side."""
    base = rates.cost_per_page_color if is_color else rates.cost_per_page_mono
    if is_duplex and rates.duplex_discount:
        discount = min(max(rates.duplex_discount, 0.0), 0.999)
        base *= 1.0 - discount
    return base


def job_cost(
    pages: int,
    rates: PrinterRates,
    *,
    is_color: bool = False,
    is_duplex: bool = False,
    ndigits: int = 4,
) -> float:
    """Total money cost of ``pages`` printed sides."""
    if pages <= 0:
        return 0.0
    return round(pages * per_page_rate(rates, is_color=is_color, is_duplex=is_duplex), ndigits)


def sheets_used(pages: int, *, is_duplex: bool = False, number_up: int = 1) -> int:
    """Physical sheets consumed -- useful for paper-stock reporting."""
    if pages <= 0:
        return 0
    number_up = max(int(number_up), 1)
    sides = -(-pages // number_up)  # ceil
    if is_duplex:
        return -(-sides // 2)
    return sides
