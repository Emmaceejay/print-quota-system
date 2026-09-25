"""Authentication and session handling for the web app.

Passwords are bcrypt-hashed with the ``bcrypt`` library directly (passlib's
bcrypt backend is incompatible with bcrypt >= 4 and passlib itself is no
longer maintained). Sessions are signed cookies
(itsdangerous) carrying only the username and issue time -- no server-side
session store to keep in sync, and nothing sensitive in the cookie.

``api.auth_backend`` selects the credential source:

``local``
    validate against ``users.password_hash``.
``pam``
    validate against the host's PAM stack (Linux accounts).
``ldap``
    validate against AD/LDAP (Phase 9; requires the ``ldap`` extra).
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.logging import get_logger
from ..db import session as db_session
from ..db.models import User

log = get_logger("api.auth")

#: bcrypt only consumes the first 72 bytes of a password; truncating
#: explicitly avoids a hard error on longer input.
_BCRYPT_MAX_BYTES = 72
_BCRYPT_ROUNDS = 12
_SALT = "printquota-session"
#: Generated once per process when no secret is configured, so a misconfigured
#: deployment fails safe (sessions die on restart) instead of using a constant.
_EPHEMERAL_SECRET = secrets.token_urlsafe(48)


def _encode(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage."""
    if not password:
        raise ValueError("password must not be empty")
    return bcrypt.hashpw(_encode(password), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    """Check a password against a stored hash, tolerating a missing hash."""
    if not password_hash or not password:
        return False
    try:
        return bcrypt.checkpw(_encode(password), password_hash.encode())
    except ValueError:  # pragma: no cover - malformed hash in the DB
        return False


def _secret_key() -> str:
    key = (get_settings().get("secret_key") or "").strip()
    if not key:
        log.warning("secret_key is not configured; using an ephemeral per-process key")
        return _EPHEMERAL_SECRET
    return key


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret_key(), salt=_SALT)


def issue_session(username: str) -> str:
    """Create a signed session token."""
    return _serializer().dumps({"u": username, "t": dt.datetime.now(dt.timezone.utc).isoformat()})


def read_session(token: str) -> Optional[str]:
    """Return the username in a valid token, else ``None``."""
    max_age = int(get_settings().get("api.session_max_age", 28800))
    try:
        data = _serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    username = data.get("u")
    return username if isinstance(username, str) else None


def _authenticate_pam(username: str, password: str) -> bool:  # pragma: no cover - host specific
    try:
        import pam  # type: ignore
    except ImportError:
        log.error("PAM auth requested but python-pam is not installed")
        return False
    return bool(pam.pam().authenticate(username, password, service="printquota"))


def _authenticate_ldap(username: str, password: str) -> bool:  # pragma: no cover - host specific
    from .ldap_auth import authenticate as ldap_authenticate

    return ldap_authenticate(username, password)


def authenticate(session: Session, username: str, password: str) -> Optional[User]:
    """Validate credentials and return the active user record.

    The typed name is matched like a print job's (``CORP\\J.Doe`` or
    ``J.Doe`` finds ``j.doe``); the password is then checked
    against that account's own name.
    """
    from ..services.identity import resolve_user

    user = resolve_user(session, username).user
    if user is None or not user.is_active:
        return None
    backend = str(get_settings().get("api.auth_backend", "local")).lower()
    if backend == "local":
        ok = verify_password(password, user.password_hash)
    elif backend == "pam":
        ok = _authenticate_pam(user.username, password)
    elif backend == "ldap":
        ok = _authenticate_ldap(user.username, password)
    else:
        log.error("unknown auth backend", extra={"backend": backend})
        ok = False
    return user if ok else None


def get_db() -> Session:
    """FastAPI dependency yielding a request-scoped session."""
    factory = db_session.get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def current_user(request: Request, session: Session = Depends(get_db)) -> User:
    """Dependency: the logged-in user, or 401."""
    cookie_name = str(get_settings().get("api.session_cookie", "printquota_session"))
    token = request.cookies.get(cookie_name)
    username = read_session(token) if token else None
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not signed in")
    user = session.get(User, username)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account unavailable")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    """Dependency: the logged-in user, who must be an administrator."""
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="administrator only")
    return user
