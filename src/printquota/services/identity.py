"""Match the name a print job or sign-in arrives with to a print account.

Windows does not always send the bare logon name. Depending on how the user
signed in and how the printer was added, the same person can arrive as
``j.doe``, ``J.Doe``, ``CORP\\J.Doe`` or (once the server is
domain-joined) ``j.doe@corp.example.com``. Active Directory treats all of these
as one account, so printquota does too.

Lookup order, for each form of the name (as sent, then without the
``DOMAIN\\`` prefix, then without an ``@realm`` suffix):

1. exact match -- so every name that matched before still matches;
2. case-insensitive match, only if exactly one account qualifies.

Two accounts that differ only in capitals are never guessed between: the
result is *ambiguous* and the caller refuses the job with a clear reason.
Account creation refuses such near-duplicates so this cannot arise from the
console, CLI or import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import User


@dataclass
class Resolution:
    """Outcome of matching a submitted name to an account."""

    user: Optional[User] = None
    #: How the account was found: exact | domain-stripped | case-insensitive
    #: | realm-stripped; or none | ambiguous when no account was chosen.
    how: str = "none"
    #: Accounts that matched equally well, when ``how == "ambiguous"``.
    candidates: list[str] = field(default_factory=list)

    @property
    def ambiguous(self) -> bool:
        return self.how == "ambiguous"


def name_forms(raw: str) -> list[tuple[str, str]]:
    """The forms of a submitted name to try, each with a label for logging."""
    name = (raw or "").strip()
    forms: list[tuple[str, str]] = []
    if not name:
        return forms
    forms.append((name, "as sent"))
    if "\\" in name:
        bare = name.rsplit("\\", 1)[1].strip()
        if bare:
            forms.append((bare, "domain-stripped"))
    last = forms[-1][0]
    if "@" in last:
        local = last.split("@", 1)[0].strip()
        if local:
            forms.append((local, "realm-stripped"))
    seen: set[str] = set()
    unique = []
    for form, label in forms:
        if form not in seen:
            seen.add(form)
            unique.append((form, label))
    return unique


def same_name_ignoring_case(session: Session, name: str) -> list[User]:
    """Accounts whose username equals ``name`` apart from capitals."""
    return list(session.scalars(select(User).where(func.lower(User.username) == name.lower())))


def resolve_user(session: Session, raw: str) -> Resolution:
    """Find the account a submitted name refers to."""
    for form, label in name_forms(raw):
        exact = session.get(User, form)
        if exact is not None:
            return Resolution(exact, "exact" if label == "as sent" else label)
        matches = same_name_ignoring_case(session, form)
        if len(matches) == 1:
            return Resolution(matches[0], "case-insensitive" if label == "as sent" else label)
        if len(matches) > 1:
            return Resolution(None, "ambiguous", sorted(u.username for u in matches))
    return Resolution()


def conflicting_account(session: Session, username: str) -> Optional[str]:
    """An existing account that ``username`` would clash with, if any.

    Used before creating an account: ``J.Doe`` clashes with an
    existing ``j.doe`` because print jobs could not tell them apart.
    """
    matches = same_name_ignoring_case(session, username.strip())
    return matches[0].username if matches else None


#: Characters allowed in a new account name. No backslash: store the bare
#: logon name (j.doe) and let DOMAIN\j.doe match it.
ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9._@\-]{1,128}$")


def new_account_problem(session: Session, username: str) -> Optional[str]:
    """Why ``username`` cannot be used for a new account, or ``None``."""
    name = (username or "").strip()
    if not name:
        return "Username is required."
    if "\\" in name:
        bare = name.rsplit("\\", 1)[1]
        return (
            f"Use the logon name without the domain: '{bare}' instead of '{name}'. "
            f"Print jobs sent as '{name}' are matched to '{bare}' automatically."
        )
    if not ACCOUNT_NAME_RE.fullmatch(name):
        return "Usernames may use letters, digits and . _ @ - (no spaces)."
    clash = conflicting_account(session, name)
    if clash == name:
        return f"User '{name}' already exists."
    if clash:
        return (
            f"'{name}' is the same account as the existing '{clash}': names are matched "
            "regardless of capital letters."
        )
    return None
