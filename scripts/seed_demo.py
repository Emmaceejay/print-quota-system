#!/usr/bin/env python3
"""Seed a demo dataset -- useful for evaluating the console before rollout.

    PRINTQUOTA_DB_URL=sqlite:///./demo.db python scripts/seed_demo.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from printquota.api.auth import hash_password  # noqa: E402
from printquota.db import session as db_session  # noqa: E402
from printquota.db.models import Group, Printer, User  # noqa: E402
from printquota.policies.engine import JobContext  # noqa: E402
from printquota.services.quota import authorize_job, charge_job  # noqa: E402

GROUPS = [("finance", 2000), ("engineering", 6000), ("reception", None)]
PRINTERS = [
    ("hp-mono", "socket://10.0.0.5:9100", 2.0, 0.0, True, 0.5),
    ("color-mfp", "ipp://10.0.0.6/ipp/print", 2.0, 10.0, True, 0.4),
]
USERS = [
    ("ceejay", "Ceejay", "finance", 500, True),
    ("ada", "Ada Obi", "engineering", 800, False),
    ("tunde", "Tunde Bello", "engineering", 800, False),
    ("reception", "Front Desk", "reception", 200, False),
]


def main() -> int:
    db_session.create_all()
    with db_session.session_scope() as session:
        for name, budget in GROUPS:
            if session.get(Group, name) is None:
                session.add(Group(name=name, shared_quota=budget))
        for name, uri, mono, color, duplex, discount in PRINTERS:
            if session.get(Printer, name) is None:
                session.add(
                    Printer(
                        name=name,
                        real_device_uri=uri,
                        cost_per_page_mono=mono,
                        cost_per_page_color=color,
                        supports_duplex=duplex,
                        duplex_discount=discount,
                    )
                )
        for username, display, group, quota, is_admin in USERS:
            if session.get(User, username) is None:
                session.add(
                    User(
                        username=username,
                        display_name=display,
                        email=f"{username}@example.com",
                        group_name=group,
                        quota_limit=quota,
                        low_balance_threshold=max(10, quota // 10),
                        is_admin=is_admin,
                        password_hash=hash_password("printquota"),
                    )
                )

    random.seed(7)
    titles = ["Invoice batch", "Onboarding pack", "Design draft", "Weekly report", "Contract"]
    for index in range(60):
        username = random.choice([u[0] for u in USERS])
        printer = random.choice([p[0] for p in PRINTERS])
        pages = random.randint(1, 25)
        with db_session.session_scope() as session:
            _, job = authorize_job(
                session,
                JobContext(
                    username=username,
                    printer=printer,
                    estimated_pages=pages,
                    copies=1,
                    is_color=printer == "color-mfp" and random.random() < 0.4,
                    is_duplex=random.random() < 0.5,
                    title=random.choice(titles),
                ),
                cups_job_id=1000 + index,
            )
            job_id, allowed = job.id, job.status == "allowed"
        if allowed and random.random() < 0.9:
            with db_session.session_scope() as session:
                from printquota.db.models import PrintJob

                record = session.get(PrintJob, job_id)
                charge_job(session, record, max(1, pages + random.randint(-2, 2)))

    print("demo data seeded; every account's password is 'printquota'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
