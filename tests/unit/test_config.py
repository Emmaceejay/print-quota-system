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
