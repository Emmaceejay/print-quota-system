"""AD/LDAP authentication and group sync (Phase 9).

Kept behind the ``ldap`` extra so the core install has no LDAP dependency.
Configure under an ``ldap:`` section in settings.yaml::

    ldap:
      server: "ldaps://dc01.example.local"
      bind_dn: "CN=svc-printquota,OU=Service,DC=example,DC=local"
      bind_password: "..."
      user_base: "OU=Staff,DC=example,DC=local"
      user_filter: "(sAMAccountName={username})"
      group_attribute: "memberOf"
      group_map:
        "CN=Finance,OU=Groups,DC=example,DC=local": finance
"""

from __future__ import annotations

import re
from typing import Optional

from ..core.config import get_settings
from ..core.logging import get_logger

log = get_logger("api.ldap")

_SAFE_USERNAME = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")


def _escape(value: str) -> str:
    """Escape an LDAP filter value (RFC 4515)."""
    out = []
    for char in value:
        if char in "\\*()\0":
            out.append("\\%02x" % ord(char))
        else:
            out.append(char)
    return "".join(out)


def authenticate(username: str, password: str) -> bool:
    """Bind as the user to validate their password."""
    if not _SAFE_USERNAME.match(username) or not password:
        return False
    settings = get_settings()
    cfg = settings.section("ldap")
    server_uri = cfg.get("server")
    if not server_uri:
        log.error("ldap auth requested but ldap.server is not configured")
        return False
    try:
        from ldap3 import ALL, Connection, Server  # type: ignore
    except ImportError:
        log.error("ldap auth requested but ldap3 is not installed (pip install 'printquota[ldap]')")
        return False

    server = Server(server_uri, get_info=ALL)
    search_filter = cfg.get("user_filter", "(sAMAccountName={username})").format(
        username=_escape(username)
    )
    try:
        with Connection(
            server, user=cfg.get("bind_dn"), password=cfg.get("bind_password"), auto_bind=True
        ) as conn:
            conn.search(cfg["user_base"], search_filter, attributes=["distinguishedName"])
            if not conn.entries:
                return False
            user_dn = str(conn.entries[0].distinguishedName)
        with Connection(server, user=user_dn, password=password, auto_bind=True):
            return True
    except Exception as exc:  # pragma: no cover - network dependent
        log.warning("ldap authentication failed", extra={"user": username, "error": str(exc)})
        return False


def lookup_groups(username: str) -> list[str]:  # pragma: no cover - network dependent
    """Return the printquota group names a directory user maps to."""
    settings = get_settings()
    cfg = settings.section("ldap")
    mapping: dict[str, str] = cfg.get("group_map", {}) or {}
    if not cfg.get("server") or not mapping:
        return []
    try:
        from ldap3 import ALL, Connection, Server  # type: ignore
    except ImportError:
        return []
    server = Server(cfg["server"], get_info=ALL)
    search_filter = cfg.get("user_filter", "(sAMAccountName={username})").format(
        username=_escape(username)
    )
    attribute = cfg.get("group_attribute", "memberOf")
    try:
        with Connection(
            server, user=cfg.get("bind_dn"), password=cfg.get("bind_password"), auto_bind=True
        ) as conn:
            conn.search(cfg["user_base"], search_filter, attributes=[attribute])
            if not conn.entries:
                return []
            dns = [str(value) for value in conn.entries[0][attribute].values]
    except Exception as exc:
        log.warning("ldap group lookup failed", extra={"user": username, "error": str(exc)})
        return []
    return [mapping[dn] for dn in dns if dn in mapping]
