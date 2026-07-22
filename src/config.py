"""Application configuration using pydantic-settings.

Never exposes secret values. All sensitive fields are excluded from
serialization and the config-status endpoint.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────────────
    app_name: str = Field(default="migration-poc")
    app_env: str = Field(default="development")
    log_level: str = Field(default="INFO")

    # ── Database ─────────────────────────────────────────────────
    database_url: str = Field(default="sqlite:///./migration_poc.db")

    # ── Generated files ─────────────────────────────────────────────
    generated_artifacts_dir: str = Field(default="./generated")
    reports_dir: str = Field(default="./reports")

    # ── Azure (required for cloud operations) ────────────────────
    azure_tenant_id: str = Field(default="")
    azure_client_id: str = Field(default="")
    azure_client_secret: str = Field(default="")
    azure_subscription_id: str = Field(default="")
    azure_resource_group: str = Field(default="")
    azure_location: str = Field(default="")

    # ── Azure Data Factory discovery (Phase 9, read-only) ────────
    # Real Azure discovery is DISABLED by default. It only runs when
    # enable_azure_discovery is true AND the discovery settings are set.
    azure_data_factory_name: str = Field(default="")
    enable_azure_discovery: bool = Field(default=False)
    azure_discovery_timeout_seconds: int = Field(default=60)

    # ── Fabric ───────────────────────────────────────────────────
    fabric_workspace_id: str = Field(default="")

    # ── Migration safety ─────────────────────────────────────────
    migration_dry_run: bool = Field(default=True)
    migration_require_approval: bool = Field(default=True)
    migration_allow_delete: bool = Field(default=False)

    # ── Secret fields (never serialized) ─────────────────────────
    SECRET_FIELDS: set[str] = {
        "azure_client_secret",
        "database_url",
    }

    def safe_dict(self) -> dict:
        """Return settings dict with secret values redacted."""
        data = self.model_dump()
        for key in self.SECRET_FIELDS:
            if key in data:
                data[key] = "***REDACTED***"
        # Remove internal fields
        data.pop("SECRET_FIELDS", None)
        return data

    def get_missing_azure_settings(self) -> list[str]:
        """Return list of Azure/Fabric settings that are empty."""
        required = [
            "azure_tenant_id",
            "azure_client_id",
            "azure_client_secret",
            "azure_subscription_id",
            "azure_resource_group",
            "azure_location",
            "fabric_workspace_id",
        ]
        return [name for name in required if not getattr(self, name, "")]

    def get_missing_azure_discovery_settings(self) -> list[str]:
        """Return the read-only-discovery settings that are still empty."""
        required = [
            "azure_tenant_id",
            "azure_client_id",
            "azure_client_secret",
            "azure_subscription_id",
            "azure_resource_group",
            "azure_data_factory_name",
        ]
        return [name for name in required if not getattr(self, name, "")]

    def azure_discovery_ready(self) -> bool:
        """True only when discovery is enabled and fully configured."""
        return self.enable_azure_discovery and not self.get_missing_azure_discovery_settings()


@lru_cache
def get_settings() -> Settings:
    """Cached singleton for application settings."""
    return Settings()
