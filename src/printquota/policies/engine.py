"""Policy and quota evaluation.

Everything in this module is a pure function over plain data so it can be
unit-tested without a database, a printer or CUPS. The CUPS backend and the
web app both call :func:`evaluate` and act on the returned :class:`Decision`.

Rule precedence: the most specific scope wins per ``rule_type`` --
``user`` > ``group`` > ``printer`` > ``global``. A more specific rule
completely replaces the less specific one rather than stacking with it,
which is what an administrator intuitively expects when they add an
exception for one person.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

SCOPE_PRECEDENCE = {"user": 3, "group": 2, "printer": 1, "global": 0}

#: Rules understood by the engine. Unknown rule types are ignored (and
#: reported by :func:`validate_rule`) so that a future rule type in the
#: database can never hard-fail a running backend.
KNOWN_RULES = (
    "block_color",
    "force_duplex",
    "max_pages_per_job",
    "max_copies_per_job",
    "block_filetype",
    "deny_printer",
)

_TRUTHY = {"1", "true", "yes", "on", ""}


@dataclass(frozen=True)
class PolicyRule:
    """A policy row, decoupled from the ORM."""

    scope_type: str
    scope_value: Optional[str]
    rule_type: str
    rule_value: Optional[str] = None
    is_active: bool = True

    @classmethod
    def from_orm(cls, row) -> "PolicyRule":
        return cls(
            scope_type=row.scope_type,
            scope_value=row.scope_value,
            rule_type=row.rule_type,
            rule_value=row.rule_value,
            is_active=bool(row.is_active),
        )


@dataclass(frozen=True)
class JobContext:
    """Everything the engine needs to know about a pending job."""

    username: str
    printer: str
    estimated_pages: int
    copies: int = 1
    is_color: bool = False
    is_duplex: bool = False
    filetype: Optional[str] = None
    group_name: Optional[str] = None
    title: Optional[str] = None


@dataclass(frozen=True)
class QuotaState:
    """The balances a job is charged against."""

    user_limit: int
    user_used: int
    group_shared_quota: Optional[int] = None
    group_used: int = 0
    group_name: Optional[str] = None

    @property
    def user_remaining(self) -> int:
        return self.user_limit - self.user_used

    @property
    def group_remaining(self) -> Optional[int]:
        if self.group_shared_quota is None:
            return None
        return self.group_shared_quota - self.group_used


@dataclass
class Decision:
    """Outcome of evaluating policies and quota for a job."""

    allowed: bool
    reason: Optional[str] = None
    rule: Optional[str] = None
    #: CUPS options the backend should force on the job, e.g. duplex.
    forced_options: dict[str, str] = field(default_factory=dict)
    #: Non-fatal notes, logged with the decision.
    notes: list[str] = field(default_factory=list)

    def deny(self, reason: str, rule: str) -> "Decision":
        self.allowed = False
        self.reason = reason
        self.rule = rule
        return self


def validate_rule(rule_type: str, rule_value: Optional[str]) -> None:
    """Raise ``ValueError`` if a rule would be meaningless at evaluation time."""
    if rule_type not in KNOWN_RULES:
        raise ValueError(f"unknown rule_type {rule_type!r}; known: {', '.join(KNOWN_RULES)}")
    if rule_type in ("max_pages_per_job", "max_copies_per_job"):
        if rule_value is None or not str(rule_value).strip().isdigit():
            raise ValueError(f"{rule_type} requires a positive integer rule_value")
        if int(rule_value) <= 0:
            raise ValueError(f"{rule_type} requires a positive integer rule_value")
    if rule_type == "block_filetype" and not (rule_value or "").strip():
        raise ValueError("block_filetype requires a comma-separated list of extensions")


def _rule_applies(rule: PolicyRule, ctx: JobContext) -> bool:
    if not rule.is_active:
        return False
    if rule.scope_type == "global":
        return True
    if rule.scope_type == "user":
        return rule.scope_value == ctx.username
    if rule.scope_type == "group":
        return ctx.group_name is not None and rule.scope_value == ctx.group_name
    if rule.scope_type == "printer":
        return rule.scope_value == ctx.printer
    return False


def resolve_rules(rules: Iterable[PolicyRule], ctx: JobContext) -> dict[str, PolicyRule]:
    """Return the winning rule per ``rule_type`` for this job context."""
    winners: dict[str, PolicyRule] = {}
    for rule in rules:
        if not _rule_applies(rule, ctx):
            continue
        current = winners.get(rule.rule_type)
        if current is None or SCOPE_PRECEDENCE.get(rule.scope_type, -1) > SCOPE_PRECEDENCE.get(
            current.scope_type, -1
        ):
            winners[rule.rule_type] = rule
    return winners


def _is_on(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def _normalise_extensions(raw: Optional[str]) -> set[str]:
    return {
        part.strip().lower().lstrip(".")
        for part in (raw or "").split(",")
        if part.strip()
    }


def evaluate_policies(rules: Sequence[PolicyRule], ctx: JobContext) -> Decision:
    """Apply policy rules only (no quota arithmetic)."""
    decision = Decision(allowed=True)
    winners = resolve_rules(rules, ctx)

    rule = winners.get("deny_printer")
    if rule is not None and _is_on(rule.rule_value):
        return decision.deny(
            f"printing to '{ctx.printer}' is not permitted for {ctx.username}", "deny_printer"
        )

    rule = winners.get("block_color")
    if rule is not None and _is_on(rule.rule_value) and ctx.is_color:
        return decision.deny("colour printing is not permitted", "block_color")

    rule = winners.get("block_filetype")
    if rule is not None and ctx.filetype:
        blocked = _normalise_extensions(rule.rule_value)
        if ctx.filetype.strip().lower().lstrip(".") in blocked:
            return decision.deny(f"file type '{ctx.filetype}' is blocked", "block_filetype")

    rule = winners.get("max_copies_per_job")
    if rule is not None and str(rule.rule_value or "").isdigit():
        limit = int(rule.rule_value)
        if ctx.copies > limit:
            return decision.deny(
                f"{ctx.copies} copies exceeds the {limit}-copy per-job limit",
                "max_copies_per_job",
            )

    rule = winners.get("max_pages_per_job")
    if rule is not None and str(rule.rule_value or "").isdigit():
        limit = int(rule.rule_value)
        if ctx.estimated_pages > limit:
            return decision.deny(
                f"{ctx.estimated_pages} pages exceeds the {limit}-page per-job limit",
                "max_pages_per_job",
            )

    rule = winners.get("force_duplex")
    if rule is not None and _is_on(rule.rule_value) and not ctx.is_duplex:
        decision.forced_options["sides"] = "two-sided-long-edge"
        decision.notes.append("duplex forced by policy")

    return decision


def evaluate_quota(
    state: QuotaState,
    pages: int,
    *,
    enforcement: str = "strict",
    enforce_group_budget: bool = True,
) -> Decision:
    """Check a page count against the user's quota and the group pool."""
    decision = Decision(allowed=True)
    if enforcement == "soft":
        if state.user_remaining < pages:
            decision.notes.append("over quota, allowed by soft enforcement")
        return decision

    if state.user_remaining < pages:
        return decision.deny(
            f"quota exceeded: {pages} page(s) requested, "
            f"{max(state.user_remaining, 0)} remaining of {state.user_limit}",
            "user_quota",
        )

    if enforce_group_budget and state.group_remaining is not None:
        if state.group_remaining < pages:
            return decision.deny(
                f"group '{state.group_name}' budget exceeded: {pages} page(s) requested, "
                f"{max(state.group_remaining, 0)} remaining of {state.group_shared_quota}",
                "group_quota",
            )
    return decision


def evaluate(
    rules: Sequence[PolicyRule],
    ctx: JobContext,
    state: QuotaState,
    *,
    enforcement: str = "strict",
    enforce_group_budget: bool = True,
) -> Decision:
    """Full pre-flight evaluation: policies first, then quota.

    Policies are checked first on purpose -- a job that violates a hard
    policy should be reported as a policy denial, not as a quota denial,
    and should not consume the user's attention with a balance message.
    """
    decision = evaluate_policies(rules, ctx)
    if not decision.allowed:
        return decision

    pages = max(int(ctx.estimated_pages), 0)
    quota_decision = evaluate_quota(
        state,
        pages,
        enforcement=enforcement,
        enforce_group_budget=enforce_group_budget,
    )
    if not quota_decision.allowed:
        quota_decision.forced_options = decision.forced_options
        quota_decision.notes = decision.notes + quota_decision.notes
        return quota_decision

    decision.notes.extend(quota_decision.notes)
    return decision
