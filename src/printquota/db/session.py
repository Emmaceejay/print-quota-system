"""Engine and session management.

The engine is built from ``database.url``. For SQLite we turn on WAL and
foreign-key enforcement and give writers a busy timeout, because the CUPS
backend, the accounting daemon and the web app all touch the same file.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..core.config import get_settings
from .models import Base

_engine: Optional[Engine] = None
_SessionFactory: Optional[sessionmaker[Session]] = None


def _apply_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()


def build_engine(url: str | None = None, echo: bool | None = None) -> Engine:
    """Create a new engine for ``url`` (defaults to the configured URL)."""
    settings = get_settings()
    url = url or settings.get("database.url")
    echo = settings.get("database.echo", False) if echo is None else echo
    kwargs: dict = {"echo": echo, "future": True}
    if url.startswith("sqlite"):
        # Ensure the parent directory exists for file-backed SQLite.
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"timeout": 10, "check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        _apply_sqlite_pragmas(engine)
    return engine


def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            bind=get_engine(), expire_on_commit=False, future=True
        )
    return _SessionFactory


def configure(engine: Engine) -> None:
    """Point the module at a specific engine (used by tests and the CLI)."""
    global _engine, _SessionFactory
    _engine = engine
    _SessionFactory = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def reset() -> None:
    """Drop cached engine/session factory."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(engine: Engine | None = None) -> None:
    """Create the schema directly (tests and first-run bootstrap).

    Production upgrades go through Alembic; this is the equivalent of
    ``alembic upgrade head`` on an empty database.
    """
    Base.metadata.create_all(engine or get_engine())
