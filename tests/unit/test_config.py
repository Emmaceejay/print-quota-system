"""Configuration loading and precedence."""

from __future__ import annotations

import pytest
import yaml

from printquota.core.config import DEFAULTS, load_settings
from printquota.core.exceptions import ConfigError


def test_defaults_used_when_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRINTQUOTA_CONFIG", raising=False)
    settings = load_settings()
    assert settings.get("quota.period_days") == DEFAULTS["quota"]["period_days"]


def test_yaml_overrides_defaults_and_env_overrides_yaml(tmp_path, monkeypatch):
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump({"quota": {"default_limit": 42}, "database": {"url": "sqlite:///a.db"}}))
    settings = load_settings(path)
    assert settings.get("quota.default_limit") == 42
    # untouched keys keep their defaults (deep merge, not replacement)
    assert settings.get("quota.period_days") == 30

    monkeypatch.setenv("PRINTQUOTA_DB_URL", "sqlite:///b.db")
    settings = load_settings(path)
    assert settings.get("database.url") == "sqlite:///b.db"


def test_env_values_are_coerced_to_the_default_type(tmp_path, monkeypatch):
    monkeypatch.setenv("PRINTQUOTA_SMTP_PORT", "2525")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRINTQUOTA_CONFIG", raising=False)
    settings = load_settings()
    assert settings.get("alerts.smtp.port") == 2525
    assert isinstance(settings.get("alerts.smtp.port"), int)


def test_missing_explicit_config_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.yaml")


def test_require_rejects_empty_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRINTQUOTA_CONFIG", raising=False)
    settings = load_settings()
    with pytest.raises(ConfigError):
        settings.require("secret_key")


def test_console_overrides_sit_between_the_file_and_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump({"quota": {"default_limit": 42}, "alerts": {"smtp": {"port": 25}}}))
    monkeypatch.delenv("PRINTQUOTA_SMTP_PORT", raising=False)

    settings = load_settings(path, overrides={"quota.default_limit": 99, "alerts.smtp.port": 587})
    assert settings.get("quota.default_limit") == 99
    assert settings.get("alerts.smtp.port") == 587

    monkeypatch.setenv("PRINTQUOTA_SMTP_PORT", "2525")
    settings = load_settings(path, overrides={"alerts.smtp.port": 587})
    assert settings.get("alerts.smtp.port") == 2525


def test_overrides_cannot_touch_infrastructure_settings(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump({"database": {"url": "sqlite:///a.db"}}))
    settings = load_settings(
        path, overrides={"database.url": "sqlite:///evil.db", "secret_key": "x", "api.auth_backend": "pam"}
    )
    assert settings.get("database.url") == "sqlite:///a.db"
    assert settings.get("secret_key") == ""
    assert settings.get("api.auth_backend") == "local"


def test_runtime_keys_and_console_fields_stay_in_step():
    from printquota.core.config import RUNTIME_KEYS
    from printquota.services.runtime_settings import FIELDS

    assert {f.key for f in FIELDS} == set(RUNTIME_KEYS)
    kinds = {"int": int, "float": float, "bool": bool}
    for spec in FIELDS:
        expected = kinds.get(spec.kind, str)
        assert RUNTIME_KEYS[spec.key] is expected, spec.key
        default = DEFAULTS
        for part in spec.key.split("."):
            default = default[part]
        assert isinstance(default, expected), spec.key
