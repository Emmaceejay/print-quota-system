"""Account lifecycle helpers shared by the CLI and the web console."""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..db.models import PrintJob, PrintPolicy, User


def delete_user(session: Session, user: User) -> int:
    """Delete an account, its job history and the policies that target it.

    ``print_jobs.username`` is NOT NULL, so the jobs are deleted explicitly
    rather than left for the ORM to orphan. Returns the number of jobs removed.
    """
    jobs = session.execute(
        delete(PrintJob).where(PrintJob.username == user.username).execution_options(
            synchronize_session="fetch"
        )
    ).rowcount or 0
    session.execute(
        delete(PrintPolicy)
        .where(PrintPolicy.scope_type == "user", PrintPolicy.scope_value == user.username)
        .execution_options(synchronize_session="fetch")
    )
    session.expire(user, ["jobs"])
    session.delete(user)
    return jobs
