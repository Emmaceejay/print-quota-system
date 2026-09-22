"""The CUPS wrapper backend, driven exactly as CUPS would drive it."""

from __future__ import annotations

import json
import os
import stat

import pytest

from printquota.backend import quota_backend as qb
from printquota.db import session as db_session
from printquota.db.models import PrintJob, User


@pytest.fixture()
def fake_backend(seeded):
    """Install a stand-in for the real CUPS backend that records its argv."""
    record = seeded["tmp_path"] / "invocation.json"
    script = seeded["backend_dir"] / "socket"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"json.dump({{'argv': sys.argv, 'device_uri': os.environ.get('DEVICE_URI')}}, open({str(record)!r}, 'w'))\n"
        "sys.exit(0)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    seeded["record"] = record
    return seeded


def spool(env, name: str = "doc.txt", lines: int = 120):
    path = env["tmp_path"] / name
    path.write_text("line\n" * lines)
    return path


def cups_argv(path, job_id="41", user="ceejay", copies="1", options="") -> list[str]:
    return ["quota", job_id, user, "Quarterly report", copies, options, str(path)]


def cups_env(env, printer="hp-mono", device_uri="quota:socket://10.0.0.5:9100") -> dict:
    return {**os.environ, "PRINTER": printer, "DEVICE_URI": device_uri}


def test_no_arguments_prints_a_discovery_line(seeded, capsys):
    assert qb.run(["quota"]) == qb.CUPS_BACKEND_OK
    assert "printquota" in capsys.readouterr().out


def test_wrong_argument_count_fails(seeded):
    assert qb.run(["quota", "1", "2"]) == qb.CUPS_BACKEND_FAILED


def test_allowed_job_is_handed_to_the_real_backend(fake_backend):
    path = spool(fake_backend)
    rc = qb.run(cups_argv(path), cups_env(fake_backend))
    assert rc == qb.CUPS_BACKEND_OK
    invocation = json.loads(fake_backend["record"].read_text())
    assert invocation["device_uri"] == "socket://10.0.0.5:9100"
    assert invocation["argv"][1:5] == ["41", "ceejay", "Quarterly report", "1"]
    with db_session.session_scope() as session:
        job = session.query(PrintJob).one()
        assert job.status == PrintJob.STATUS_ALLOWED
        assert job.cups_job_id == 41 and job.estimated_pages == 2
        assert session.get(User, "ceejay").pages_used == 2


def test_denied_job_is_cancelled_and_never_reaches_the_printer(fake_backend, capsys):
    with db_session.session_scope() as session:
        session.get(User, "ceejay").quota_limit = 1
    path = spool(fake_backend)
    rc = qb.run(cups_argv(path), cups_env(fake_backend))
    assert rc == qb.CUPS_BACKEND_CANCEL, "a denial must cancel the job, not stop the queue"
    assert not fake_backend["record"].exists()
    assert "denied" in capsys.readouterr().err


def test_options_are_parsed_into_colour_and_duplex_flags(fake_backend):
    path = spool(fake_backend)
    qb.run(
        cups_argv(path, options="sides=two-sided-long-edge print-color-mode=color number-up=2"),
        cups_env(fake_backend, printer="color-mfp"),
    )
    with db_session.session_scope() as session:
        job = session.query(PrintJob).one()
        assert job.is_color and job.is_duplex
        assert job.estimated_pages == 1  # 2 pages, 2-up


def test_force_duplex_policy_is_appended_to_the_child_options(fake_backend):
    from printquota.db.models import PrintPolicy

    with db_session.session_scope() as session:
        session.add(
            PrintPolicy(scope_type="printer", scope_value="hp-mono", rule_type="force_duplex", rule_value="true")
        )
    path = spool(fake_backend)
    qb.run(cups_argv(path, options="media=A4"), cups_env(fake_backend))
    invocation = json.loads(fake_backend["record"].read_text())
    assert "sides=two-sided-long-edge" in invocation["argv"][5]


def test_unknown_user_is_denied(fake_backend):
    path = spool(fake_backend)
    rc = qb.run(cups_argv(path, user="stranger"), cups_env(fake_backend))
    assert rc == qb.CUPS_BACKEND_CANCEL
    assert not fake_backend["record"].exists()


def test_a_broken_device_uri_stops_the_queue_rather_than_printing_free(fake_backend):
    path = spool(fake_backend)
    rc = qb.run(cups_argv(path), cups_env(fake_backend, device_uri="quota:nosuchbackend://x"))
    assert rc == qb.CUPS_BACKEND_STOP


def test_datastore_failure_holds_the_job(fake_backend, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(qb.db_session, "session_scope", boom)
    path = spool(fake_backend)
    assert qb.run(cups_argv(path), cups_env(fake_backend)) == qb.CUPS_BACKEND_HOLD


def test_device_uri_parsing():
    assert qb.split_device_uri("quota:socket://10.0.0.5:9100") == "socket://10.0.0.5:9100"
    assert qb.split_device_uri("quota://ipp://host/ipp/print") == "ipp://host/ipp/print"
    with pytest.raises(ValueError):
        qb.split_device_uri("socket://10.0.0.5:9100")
    with pytest.raises(ValueError):
        qb.split_device_uri("")


def test_real_backend_path_rejects_traversal(seeded):
    with pytest.raises(ValueError):
        qb.real_backend_path("../../bin/sh://x", str(seeded["backend_dir"]))
    with pytest.raises(ValueError):
        qb.real_backend_path("missing://x", str(seeded["backend_dir"]))
