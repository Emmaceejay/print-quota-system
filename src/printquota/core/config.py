"""Configuration loading.

Precedence (highest first):
    1. ``PRINTQUOTA_*`` environment variables
    2. ``config/settings.yaml`` (path from ``PRINTQUOTA_CONFIG``)
    3. Built-in defaults in :data:`DEFAULTS`

Nothing operational is hardcoded at the call sites; every component reads
its values through :func:`get_settings`.
"""

from __future__ import annotations

import copy
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .exceptions import ConfigError

DEFAULTS: dict[str, Any] = {
    "database": {"url": "sqlite:///./printquota.db", "echo": False},
    "quota": {
        "default_limit": 500,
        "period_days": 30,
        "default_low_balance_threshold": 50,
        "enforcement": "strict",
        "enforce_group_budget": True,
    },
    "printing": {
        "default_cost_per_page_mono": 1.0,
        "default_cost_per_page_color": 5.0,
        "default_duplex_discount": 0.0,
        "currency": "NGN",
        "real_backend_dir": "/usr/lib/cups/backend",
        "page_log": "/var/log/cups/page_log",
        "estimator_timeout": 15,
    },
    "alerts": {
        "enabled": True,
        "channel": "email",
        "webhook_url": "",
        "cooldown_hours": 24,
        "smtp": {
            "host": "",
            "port": 25,
            "user": "",
            "password": "",
            "from_address": "printquota@localhost",
            "use_tls": False,
        },
    },
    "api": {
        "host": "0.0.0.0",
        "port": 8080,
        "session_cookie": "printquota_session",
        "session_max_age": 28800,
        "auth_backend": "local",
    },
    "logging": {"level": "INFO", "use_journald": True},
    "secret_key": "",
}

#: Environment variable -> dotted settings path.
ENV_MAP: dict[str, str] = {
    "PRINTQUOTA_DB_URL": "database.url",
    "PRINTQUOTA_DB_ECHO": "database.echo",
    "PRINTQUOTA_SECRET_KEY": "secret_key",
    "PRINTQUOTA_LOG_LEVEL": "logging.level",
    "PRINTQUOTA_PAGE_LOG": "printing.page_log",
    "PRINTQUOTA_REAL_BACKEND_DIR": "printing.real_backend_dir",
    "PRINTQUOTA_SMTP_HOST": "alerts.smtp.host",
    "PRINTQUOTA_SMTP_PORT": "alerts.smtp.port",
    "PRINTQUOTA_SMTP_USER": "alerts.smtp.user",
    "PRINTQUOTA_SMTP_PASSWORD": "alerts.smtp.password",
    "PRINTQUOTA_SMTP_FROM": "alerts.smtp.from_address",
    "PRINTQUOTA_API_HOST": "api.host",
    "PRINTQUOTA_API_PORT": "api.port",
    "PRINTQUOTA_AUTH_BACKEND": "api.auth_backend",
}

_CONFIG_SEARCH = (
    "/etc/printquota/settings.yaml",
    "config/settings.yaml",
)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce(existing: Any, raw: str) -> Any:
    """Coerce an environment string to the type of the default it replaces."""
    if isinstance(existing, bool):
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ConfigError(f"expected a boolean, got {raw!r}")
    if isinstance(existing, int) and not isinstance(existing, bool):
        return int(raw)
    if isinstance(existing, float):
        return float(raw)
    return raw


def _set_path(data: dict[str, Any], dotted: str, raw: str) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    leaf = parts[-1]
    node[leaf] = _coerce(node.get(leaf), raw)


class Settings:
    """Read-only view over the merged configuration tree."""

    def __init__(self, data: dict[str, Any], source: str | None = None) -> None:
        self._data = data
        self.source = source

    def get(self, dotted: str, default: Any = None) -> Any:
        """Return a value by dotted path, e.g. ``quota.period_days``."""
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        value = self.get(dotted, None)
        if value in (None, ""):
            raise ConfigError(f"required setting {dotted!r} is not set")
        return value

    def section(self, name: str) -> dict[str, Any]:
        value = self.get(name, {})
        return copy.deepcopy(value) if isinstance(value, dict) else {}

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Settings source={self.source!r}>"


def _locate_config(path: str | os.PathLike[str] | None) -> Path | None:
    if path:
        candidate = Path(path)
        if not candidate.is_file():
            raise ConfigError(f"config file not found: {candidate}")
        return candidate
    env_path = os.environ.get("PRINTQUOTA_CONFIG")
    if env_path:
        candidate = Path(env_path)
        if not candidate.is_file():
            raise ConfigError(f"PRINTQUOTA_CONFIG points at a missing file: {candidate}")
        return candidate
    for name in _CONFIG_SEARCH:
        candidate = Path(name)
        if candidate.is_file():
            return candidate
    return None


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Build a :class:`Settings` from defaults, YAML file and environment."""
    config_path = _locate_config(path)
    data = copy.deepcopy(DEFAULTS)
    if config_path is not None:
        try:
            loaded = yaml.safe_load(config_path.read_text()) or {}
        except yaml.YAMLError as exc:  # pragma: no cover - malformed file
            raise ConfigError(f"could not parse {config_path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"{config_path} must contain a YAML mapping")
        data = _deep_merge(data, loaded)
    for env_name, dotted in ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw is not None and raw != "":
            _set_path(data, dotted, raw)
    return Settings(data, source=str(config_path) if config_path else "defaults")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings."""
    return load_settings()


def reset_settings_cache() -> None:
    """Drop the cached settings (used by tests and by the CLI ``--config``)."""
    get_settings.cache_clear()
