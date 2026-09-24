"""Accounting daemon: page_log parsing, cursor handling, reconciliation."""

from __future__ import annotations

from printquota.accounting import daemon
from printquota.db import session as db_session
from printquota.db.models import Group, PrintJob, User
from printquota.policies.engine import JobContext
from printquota.services.quota import authorize_job

LINE = "{printer} {user} {job} [22/Sep/2026:10:00:0{n} +0100] {page} {copies} - localhost report.pdf A4 {sides}"


def page_lines(printer="hp-mono", user="ceejay", job=41, pages=3, copies=1, sides="one-sided"):
    return [
        LINE.format(printer=printer, user=user, job=job, n=index % 10, page=index, copies=copies, sides=sides)
        + "\n"
        for index in range(1, pages + 1)
    ]


def make_job(pages: int = 3, cups_job_id: int = 41) -> int:
    with db_session.session_scope() as session:
        _, job = authorize_job(
            session,
            JobContext(username="ceejay", printer="hp-mono", estimated_pages=pages),
            cups_job_id=cups_job_id,
        )
        return job.id


def test_parses_a_default_format_page_log_line():
    tally = daemon.parse_page_log_line(page_lines()[0])
    assert tally is not None
    assert (tally.printer, tally.username, tally.cups_job_id, tally.pages) == ("hp-mono", "ceejay", 41, 1)


def test_ignores_unparseable_lines():
    assert daemon.parse_page_log_line("garbage") is None


def test_a_total_line_is_used_only_when_there_are_no_page_lines():
    total = page_lines()[0].replace(" 1 1 ", " total 3 ")
    parsed = daemon.parse_page_log_line(total)
    assert parsed.pages == 0 and parsed.total == 3 and parsed.billed_pages == 3

    only_total = daemon.aggregate([total])
    assert only_total[("hp-mono", 41)].billed_pages == 3

    both = daemon.aggregate(page_lines(pages=3) + [total])
    assert both[("hp-mono", 41)].billed_pages == 3  # not 6: the total duplicates the pages


def test_reconciles_from_a_total_line(seeded):
    job_id = make_job(pages=1)
    daemon.reconcile_tallies(daemon.aggregate([page_lines()[0].replace(" 1 1 ", " total 4 ")]))
    with db_session.session_scope() as session:
        job = session.get(PrintJob, job_id)
        assert job.actual_pages == 4 and job.reconciled
        assert session.get(User, "ceejay").pages_used == 4


def test_copies_column_multiplies_the_page_count():
    tallies = daemon.aggregate(page_lines(pages=2, copies=3))
    assert tallies[("hp-mono", 41)].pages == 6


def test_duplex_is_detected_from_the_sides_column():
    tallies = daemon.aggregate(page_lines(sides="two-sided-long-edge"))
    assert tallies[("hp-mono", 41)].is_duplex


def test_reconciles_an_allowed_job_from_the_log(seeded):
    job_id = make_job(pages=3)
    seeded["page_log"].write_text("".join(page_lines(pages=5)))
    assert daemon.run_once(seeded["page_log"], seeded["tmp_path"] / "state.json") == 1
    with db_session.session_scope() as session:
        job = session.get(PrintJob, job_id)
        assert job.status == PrintJob.STATUS_COMPLETED and job.actual_pages == 5
        assert session.get(User, "ceejay").pages_used == 5
        assert session.get(Group, "finance").pages_used == 5


def test_the_cursor_stops_the_same_lines_being_charged_twice(seeded):
    make_job(pages=3)
    state = seeded["tmp_path"] / "state.json"
    seeded["page_log"].write_text("".join(page_lines(pages=5)))
    assert daemon.run_once(seeded["page_log"], state) == 1
    assert daemon.run_once(seeded["page_log"], state) == 0
    with db_session.session_scope() as session:
        assert session.get(User, "ceejay").pages_used == 5


def test_new_lines_appended_later_are_picked_up(seeded):
    make_job(pages=1, cups_job_id=41)
    make_job(pages=1, cups_job_id=42)
    state = seeded["tmp_path"] / "state.json"
    seeded["page_log"].write_text("".join(page_lines(job=41, pages=2)))
    daemon.run_once(seeded["page_log"], state)
    with seeded["page_log"].open("a") as handle:
        handle.write("".join(page_lines(job=42, pages=4)))
    assert daemon.run_once(seeded["page_log"], state) == 1
    with db_session.session_scope() as session:
        assert session.get(User, "ceejay").pages_used == 6


def test_log_rotation_is_detected_and_not_replayed_from_the_old_offset(seeded):
    make_job(pages=1, cups_job_id=41)
    state = seeded["tmp_path"] / "state.json"
    seeded["page_log"].write_text("".join(page_lines(job=41, pages=6)))
    daemon.run_once(seeded["page_log"], state)

    make_job(pages=1, cups_job_id=77)
    seeded["page_log"].unlink()
    seeded["page_log"].write_text("".join(page_lines(job=77, pages=2)))
    assert daemon.run_once(seeded["page_log"], state) == 1
    with db_session.session_scope() as session:
        assert session.get(User, "ceejay").pages_used == 8


def test_a_partial_trailing_line_is_read_on_the_next_pass(seeded):
    make_job(pages=1, cups_job_id=41)
    state = seeded["tmp_path"] / "state.json"
    complete = "".join(page_lines(job=41, pages=2))
    partial = page_lines(job=41, pages=3)[2].rstrip("\n")
    seeded["page_log"].write_text(complete + partial)
    daemon.run_once(seeded["page_log"], state)
    with db_session.session_scope() as session:
        assert session.get(PrintJob, 1).actual_pages == 2
    # the line is completed by CUPS on the next write
    seeded["page_log"].write_text(complete + partial + "\n")
    daemon.run_once(seeded["page_log"], state)


def test_log_entries_with_no_matching_job_are_skipped_not_crashed(seeded):
    seeded["page_log"].write_text("".join(page_lines(job=999)))
    assert daemon.run_once(seeded["page_log"], seeded["tmp_path"] / "state.json") == 0
