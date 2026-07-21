"""Tests for src.config — Phase 1."""

import os

import pytest

from src.config import Settings


class TestSettingsDefaults:
    """Verify default values load correctly."""

    def test_default_app_env(self):
        s = Settings(
            _env_file=None,
            azure_tenant_id="",
            azure_client_id="",
            azure_client_secret="",
            azure_subscription_id="",
            azure_resource_group="",
            azure_location="",
        )
        assert s.app_env == "development"

    def test_default_database_url(self):
        s = Settings(
            _env_file=None,
            azure_tenant_id="",
            azure_client_id="",
            azure_client_secret="",
            azure_subscription_id="",
            azure_resource_group="",
            azure_location="",
        )
        assert "sqlite" in s.database_url

    def test_default_dry_run_true(self):
        s = Settings(
            _env_file=None,
            azure_tenant_id="",
            azure_client_id="",
            azure_client_secret="",
            azure_subscription_id="",
            azure_resource_group="",
            azure_location="",
        )
        assert s.migration_dry_run is True

    def test_default_delete_allowed_false(self):
        s = Settings(
            _env_file=None,
            azure_tenant_id="",
            azure_client_id="",
            azure_client_secret="",
            azure_subscription_id="",
            azure_resource_group="",
            azure_location="",
        )
        assert s.migration_allow_delete is False

    def test_default_require_approval_true(self):
        s = Settings(
            _env_file=None,
            azure_tenant_id="",
            azure_client_id="",
            azure_client_secret="",
            azure_subscription_id="",
            azure_resource_group="",
            azure_location="",
        )
        assert s.migration_require_approval is True


class TestSettingsOverrides:
    """Verify environment variables override defaults."""

    def test_env_override_app_env(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        s = Settings(_env_file=None)
        assert s.app_env == "production"

    def test_env_override_log_level(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        s = Settings(_env_file=None)
        assert s.log_level == "DEBUG"


class TestSafeSerialization:
    """Secrets must never appear in safe_dict output."""

    def test_secrets_redacted_in_safe_dict(self):
        s = Settings(
            _env_file=None,
            azure_client_secret="super-secret-value",
            azure_tenant_id="",
            azure_client_id="",
            azure_subscription_id="",
            azure_resource_group="",
            azure_location="",
        )
        safe = s.safe_dict()
        assert safe["azure_client_secret"] == "***REDACTED***"
        assert "super-secret-value" not in str(safe)

    def test_database_url_redacted(self):
        s = Settings(
            _env_file=None,
            azure_tenant_id="",
            azure_client_id="",
            azure_client_secret="",
            azure_subscription_id="",
            azure_resource_group="",
            azure_location="",
        )
        safe = s.safe_dict()
        assert safe["database_url"] == "***REDACTED***"


class TestMissingAzure:
    """Verify detection of unconfigured Azure settings."""

    def test_all_missing_when_empty(self):
        s = Settings(
            _env_file=None,
            azure_tenant_id="",
            azure_client_id="",
            azure_client_secret="",
            azure_subscription_id="",
            azure_resource_group="",
            azure_location="",
        )
        missing = s.get_missing_azure_settings()
        assert "azure_tenant_id" in missing
        assert "fabric_workspace_id" in missing
        assert len(missing) == 7

    def test_none_missing_when_all_set(self):
        s = Settings(
            _env_file=None,
            azure_tenant_id="t",
            azure_client_id="c",
            azure_client_secret="s",
            azure_subscription_id="sub",
            azure_resource_group="rg",
            azure_location="loc",
            fabric_workspace_id="ws",
        )
        assert s.get_missing_azure_settings() == []
