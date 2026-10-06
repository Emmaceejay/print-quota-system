"""Printers & queues: CUPS queue discovery, quota enforcement and cost models.

Replaces the command-line ``register_backend.sh`` workflow for day-to-day
use. The printquota ``printers`` row is always written *before* a queue is
wrapped: the backend needs it (``print_jobs.printer`` is a foreign key), and
a wrapped queue without it would hold every job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.config import get_settings
from ...core.logging import get_logger
from ...db.models import Printer, User
from ...services import cups_queues
from ...services.audit import record_audit
from ..auth import get_db, require_admin
from ..deps import redirect, render

router = APIRouter(prefix="/admin")
log = get_logger("api.queues")


@dataclass
class QueueRow:
    name: str
    cups: Optional[cups_queues.Queue]
    printer: Optional[Printer]

    @property
    def enforced(self) -> bool:
        return bool(self.cups and self.cups.enforced)


def _backend_dir() -> str:
    return str(get_settings().get("printing.real_backend_dir", "/usr/lib/cups/backend"))


def queue_overview(session: Session) -> tuple[list[QueueRow], Optional[str]]:
    """CUPS queues merged with registered printers, plus any CUPS error."""
    printers = {p.name: p for p in session.scalars(select(Printer).order_by(Printer.name))}
    cups_error: Optional[str] = None
    try:
        queues = cups_queues.list_queues()
    except cups_queues.CupsError as exc:
        queues, cups_error = [], str(exc)
    rows = [QueueRow(q.name, q, printers.pop(q.name, None)) for q in queues]
    rows += [QueueRow(name, None, printer) for name, printer in printers.items()]
    return rows, cups_error


def _upsert_printer(session: Session, name: str, device_uri: Optional[str]) -> tuple[Printer, bool]:
    printer = session.get(Printer, name)
    created = printer is None
    if printer is None:
        settings = get_settings()
        printer = Printer(
            name=name,
            cost_per_page_mono=float(settings.get("printing.default_cost_per_page_mono", 0.0)),
            cost_per_page_color=float(settings.get("printing.default_cost_per_page_color", 0.0)),
        )
        session.add(printer)
    if device_uri:
        printer.real_device_uri = device_uri
    printer.is_active = True
    return printer, created


def apply_duplex_default(
    session: Session, admin: User, printer: Printer, two_sided: bool
) -> tuple[Optional[str], Optional[str]]:
    """Set a queue's two-sided default in CUPS, then record it.

    Returns ``(note, error)``: a note for the admin (e.g. the printer seems
    to lack a duplex unit) on success, or the error that left the setting
    unchanged.
    """
    try:
        result = cups_queues.set_duplex_default(printer.name, two_sided)
    except cups_queues.CupsError as exc:
        what = "two-sided" if two_sided else "one-sided"
        return None, f"Could not make {printer.name} print {what} by default: {exc}"
    printer.duplex_default = two_sided
    if two_sided:
        printer.supports_duplex = True
    record_audit(
        session, admin.username, "printer.duplex_default", printer.name,
        {"two_sided": two_sided, "driver_options": result.ppd_settings}, source="web",
    )
    log.info("duplex default changed", extra={"queue": printer.name, "two_sided": two_sided})
    return result.warning, None


@router.get("/printers", include_in_schema=False)
def printers_page(
    request: Request, admin: User = Depends(require_admin), session: Session = Depends(get_db)
):
    """Every CUPS queue, whether quota enforcement is on, and its cost model."""
    rows, cups_error = queue_overview(session)
    return render(
        request,
        "printers.html",
        admin=admin,
        rows=rows,
        cups_error=cups_error,
        backend_ok=cups_queues.backend_installed(_backend_dir()),
        backend_path=f"{_backend_dir()}/{cups_queues.SCHEME}",
        drivers=cups_queues.DRIVERS,
        settings=get_settings(),
    )


@router.post("/printers/{name}/enforce", include_in_schema=False)
def queue_enforce(
    name: str, admin: User = Depends(require_admin), session: Session = Depends(get_db)
):
    """Route a CUPS queue through the quota backend."""
    try:
        queue = cups_queues.get_queue(cups_queues.validate_queue_name(name))
        if queue.enforced:
            return redirect("/admin/printers", message=f"{name} is already enforced.")
        _, created = _upsert_printer(session, name, queue.device_uri)
        session.flush()
        cups_queues.enforce(name, _backend_dir())
    except cups_queues.CupsError as exc:
        session.rollback()
        return redirect("/admin/printers", error=f"Could not enforce {name}: {exc}")
    record_audit(
        session, admin.username, "queue.enforce", name,
        {"real_uri": queue.device_uri, "printer_created": created}, source="web",
    )
    log.info("queue enforced", extra={"queue": name, "admin": admin.username})
    return redirect(
        "/admin/printers",
        message=f"Quota enforcement is on for {name}. Print a test page and check Reports.",
    )


@router.post("/printers/{name}/release", include_in_schema=False)
def queue_release(
    name: str,
    confirm: bool = Form(False),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Take a queue out of quota enforcement (it keeps printing, unmetered)."""
    if not confirm:
        return redirect("/admin/printers", error=f"Tick 'confirm' to stop enforcing quotas on {name}.")
    try:
        queue = cups_queues.release(name)
    except cups_queues.CupsError as exc:
        return redirect("/admin/printers", error=f"Could not change {name}: {exc}")
    record_audit(session, admin.username, "queue.release", name, {"real_uri": queue.real_uri}, source="web")
    log.info("queue released", extra={"queue": name, "admin": admin.username})
    return redirect("/admin/printers", message=f"{name} now prints directly; quotas no longer apply to it.")


@router.post("/printers/add-queue", include_in_schema=False)
def queue_add(
    name: str = Form(...),
    device_uri: str = Form(...),
    driver: str = Form("everywhere"),
    description: str = Form(""),
    location: str = Form(""),
    cost_per_page_mono: float = Form(0.0),
    cost_per_page_color: float = Form(0.0),
    duplex_discount: float = Form(0.0),
    supports_duplex: bool = Form(False),
    duplex_default: bool = Form(False),
    enforce: bool = Form(False),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_db),
):
    """Create a new CUPS queue, register its costs, and optionally enforce it."""
    name = name.strip()
    if not 0 <= duplex_discount < 1:
        return redirect("/admin/printers", error="Duplex discount must be at least 0 and below 1.")
    try:
        cups_queues.validate_queue_name(name)
        cups_queues.validate_device_uri(device_uri)
    except cups_queues.CupsError as exc:
        return redirect("/admin/printers", error=str(exc))
    if session.get(Printer, name) is not None:
        return redirect("/admin/printers", error=f"A printer named {name} is already registered.")

    printer = Printer(
        name=name,
        description=description.strip() or None,
        real_device_uri=device_uri.strip(),
        cost_per_page_mono=max(cost_per_page_mono, 0.0),
        cost_per_page_color=max(cost_per_page_color, 0.0),
        supports_duplex=supports_duplex,
        duplex_discount=duplex_discount,
    )
    session.add(printer)
    session.flush()
    try:
        cups_queues.add_queue(name, device_uri, driver, description, location)
    except cups_queues.CupsError as exc:
        session.rollback()
        return redirect("/admin/printers", error=f"CUPS could not create {name}: {exc}")
    record_audit(
        session, admin.username, "queue.add", name,
        {"device_uri": device_uri.strip(), "driver": driver}, source="web",
    )

    problems: list[str] = []
    notes: list[str] = []
    if duplex_default:
        note, error = apply_duplex_default(session, admin, printer, True)
        problems += [error] if error else []
        notes += [note] if note else []

    if enforce:
        try:
            cups_queues.enforce(name, _backend_dir())
        except cups_queues.CupsError as exc:
            problems.append(f"Could not enforce quotas on it: {exc}")
        else:
            record_audit(
                session, admin.username, "queue.enforce", name,
                {"real_uri": device_uri.strip()}, source="web",
            )

    if problems:
        return redirect("/admin/printers", error=f"Created {name}, but: " + " ".join(problems))
    message = f"Created {name}" + (" with quota enforcement on." if enforce else
                                   ". Quota enforcement is off until you turn it on.")
    if printer.duplex_default:
        message += " It prints on both sides by default."
    return redirect("/admin/printers", message=" ".join([message, *notes]))
