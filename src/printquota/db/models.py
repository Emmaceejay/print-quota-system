"""SQLAlchemy models.

The schema is deliberately portable: no SQLite-only types, no server-side
defaults that Postgres would reject. Timestamps are stored as timezone-aware
UTC ``DateTime`` values and always written from Python so that behaviour is
identical on either backend.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    """Timezone-aware UTC now (single source of truth for all timestamps)."""
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for all printquota tables."""


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Group(TimestampMixin, Base):
    """A department or cost centre, optionally with a shared page pool."""

    __tablename__ = "groups"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    description: Mapped[Optional[str]] = mapped_column(String(255))
    #: ``None`` means the group has no shared pool; members are limited only
    #: by their own quota.
    shared_quota: Mapped[Optional[int]] = mapped_column(Integer)
    pages_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    period_start: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    users: Mapped[list["User"]] = relationship(back_populates="group")

    __table_args__ = (
        CheckConstraint("shared_quota IS NULL OR shared_quota >= 0", name="ck_groups_shared_quota"),
    )

    @property
    def remaining(self) -> Optional[int]:
        """Pages left in the shared pool, or ``None`` when unlimited."""
        if self.shared_quota is None:
            return None
        return self.shared_quota - self.pages_used

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Group {self.name}>"


class User(TimestampMixin, Base):
    """A print user, keyed on the CUPS ``job-originating-user-name``."""

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    group_name: Mapped[Optional[str]] = mapped_column(
        ForeignKey("groups.name", ondelete="SET NULL")
    )
    quota_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    pages_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Start of the current rolling period. The reset job rolls this forward
    #: by ``quota.period_days`` once it has elapsed.
    period_start: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    low_balance_threshold: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Only used when ``api.auth_backend`` is ``local``.
    password_hash: Mapped[Optional[str]] = mapped_column(String(255))

    group: Mapped[Optional[Group]] = relationship(back_populates="users")
    jobs: Mapped[list["PrintJob"]] = relationship(back_populates="user")

    __table_args__ = (
        CheckConstraint("quota_limit >= 0", name="ck_users_quota_limit"),
        Index("ix_users_group_name", "group_name"),
    )

    @property
    def remaining(self) -> int:
        """Pages left in this user's own quota (may go negative in soft mode)."""
        return self.quota_limit - self.pages_used

    def period_end(self, period_days: int) -> dt.datetime:
        start = self.period_start
        if start.tzinfo is None:
            start = start.replace(tzinfo=dt.timezone.utc)
        return start + dt.timedelta(days=period_days)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username} {self.pages_used}/{self.quota_limit}>"


class Printer(TimestampMixin, Base):
    """A CUPS queue and its cost model."""

    __tablename__ = "printers"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    description: Mapped[Optional[str]] = mapped_column(String(255))
    #: The device URI the quota backend hands the job to once it is allowed.
    real_device_uri: Mapped[Optional[str]] = mapped_column(String(512))
    cost_per_page_mono: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cost_per_page_color: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    supports_duplex: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: ``0.5`` means a duplex page costs half as much (two sides, one sheet).
    duplex_discount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "duplex_discount >= 0 AND duplex_discount < 1", name="ck_printers_duplex_discount"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Printer {self.name}>"


class PrintPolicy(TimestampMixin, Base):
    """A single policy rule, scoped to a user, group, printer or globally."""

    __tablename__ = "print_policies"

    SCOPES = ("user", "group", "printer", "global")
    RULES = (
        "block_color",
        "force_duplex",
        "max_pages_per_job",
        "max_copies_per_job",
        "block_filetype",
        "deny_printer",
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_value: Mapped[Optional[str]] = mapped_column(String(128))
    rule_type: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_value: Mapped[Optional[str]] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('user','group','printer','global')", name="ck_policies_scope_type"
        ),
        Index("ix_policies_scope", "scope_type", "scope_value"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Policy {self.scope_type}:{self.scope_value} {self.rule_type}={self.rule_value}>"


class PrintJob(Base):
    """One print job, from pre-flight decision through post-print accounting."""

    __tablename__ = "print_jobs"

    STATUS_ALLOWED = "allowed"
    STATUS_DENIED = "denied"
    STATUS_COMPLETED = "completed"
    STATUS_ERROR = "error"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: CUPS job id; unique per queue but reused after a CUPS restart, so it is
    #: not the primary key.
    cups_job_id: Mapped[Optional[int]] = mapped_column(Integer)
    username: Mapped[str] = mapped_column(
        ForeignKey("users.username", ondelete="CASCADE"), nullable=False
    )
    printer: Mapped[str] = mapped_column(
        ForeignKey("printers.name", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[Optional[str]] = mapped_column(String(512))
    copies: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_color: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_duplex: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    estimated_pages: Mapped[Optional[int]] = mapped_column(Integer)
    actual_pages: Mapped[Optional[int]] = mapped_column(Integer)
    #: Pages actually charged against the balance (reconciled by the daemon).
    charged_pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost: Mapped[Optional[float]] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    denial_reason: Mapped[Optional[str]] = mapped_column(Text)
    reconciled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    submitted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    completed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    user: Mapped[Optional[User]] = relationship(back_populates="jobs")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PrintJob {self.id} {self.username}@{self.printer} {self.status}>"


# Declared after the class body so the composite index/FK list stays readable.
PrintJob.__table__.append_constraint(
    CheckConstraint(
        "status IN ('allowed','denied','completed','error')", name="ck_jobs_status"
    )
)
Index("ix_jobs_username_submitted", PrintJob.username, PrintJob.submitted_at)
Index("ix_jobs_printer_job", PrintJob.printer, PrintJob.cups_job_id)
Index("ix_jobs_reconciled", PrintJob.reconciled, PrintJob.status)


class AlertLog(Base):
    """Record of every notification dispatched (also drives the cooldown)."""

    __tablename__ = "alerts_log"

    TYPE_LOW_BALANCE = "low_balance"
    TYPE_OVER_QUOTA = "over_quota"
    TYPE_REPEATED_DENIAL = "repeated_denial"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[Optional[str]] = mapped_column(String(128))
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[Optional[str]] = mapped_column(String(16))
    message: Mapped[Optional[str]] = mapped_column(Text)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sent_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (Index("ix_alerts_user_type_sent", "username", "alert_type", "sent_at"),)


class AdminAuditLog(Base):
    """Append-only record of privileged actions."""

    __tablename__ = "admin_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_user: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[Optional[str]] = mapped_column(String(255))
    details: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[Optional[str]] = mapped_column(String(16))
    timestamp: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (Index("ix_audit_timestamp", "timestamp"),)
