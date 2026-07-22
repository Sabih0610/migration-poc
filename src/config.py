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

    # ── Fabric deployment (Phase 10, write path, disabled by default) ─
    fabric_tenant_id: str = Field(default="")
    fabric_client_id: str = Field(default="")
    fabric_client_secret: str = Field(default="")
    fabric_capacity_id: str = Field(default="")
    fabric_deployment_enabled: bool = Field(default=False)
    fabric_api_base_url: str = Field(default="https://api.fabric.microsoft.com/v1")
    fabric_scope: str = Field(default="https://api.fabric.microsoft.com/.default")
    fabric_timeout_seconds: int = Field(default=120)

    # ── Migration safety ─────────────────────────────────────────
    migration_dry_run: bool = Field(default=True)
    migration_require_approval: bool = Field(default=True)
    migration_allow_delete: bool = Field(default=False)

    # ── Runtime execution (Phase 11, controlled source/target pipeline
    # execution + optional runtime-equivalence validation). Disabled by
    # default; never enabled by committed configuration.
    runtime_execution_enabled: bool = Field(default=False)
    adf_source_pipeline_name: str = Field(default="")
    adf_run_timeout_seconds: int = Field(default=1800)
    fabric_target_pipeline_item_id: str = Field(default="")
    fabric_run_timeout_seconds: int = Field(default=1800)
    runtime_poll_interval_seconds: int = Field(default=10)

    # ── Secret fields (never serialized) ─────────────────────────
    SECRET_FIELDS: set[str] = {
        "azure_client_secret",
        "fabric_client_secret",
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

    def get_missing_fabric_settings(self) -> list[str]:
        """Return the Fabric deployment settings that are still empty."""
        required = [
            "fabric_tenant_id",
            "fabric_client_id",
            "fabric_client_secret",
            "fabric_workspace_id",
        ]
        return [name for name in required if not getattr(self, name, "")]

    def fabric_deployment_ready(self) -> bool:
        """True only when Fabric deployment is enabled and fully configured."""
        return self.fabric_deployment_enabled and not self.get_missing_fabric_settings()

    def get_missing_runtime_execution_settings(self) -> list[str]:
        """Return the Phase 11 controlled-execution settings still empty.

        Reuses the existing Azure discovery and Fabric deployment
        credential/config requirements (Phase 9 / Phase 10) plus the
        additional source/target pipeline identity settings.
        """
        required = [
            "azure_tenant_id",
            "azure_client_id",
            "azure_client_secret",
            "azure_subscription_id",
            "azure_resource_group",
            "azure_data_factory_name",
            "adf_source_pipeline_name",
            "fabric_tenant_id",
            "fabric_client_id",
            "fabric_client_secret",
            "fabric_workspace_id",
            "fabric_target_pipeline_item_id",
        ]
        return [name for name in required if not getattr(self, name, "")]

    def runtime_execution_ready(self) -> bool:
        """True only when runtime execution is enabled and fully configured."""
        return (
            self.runtime_execution_enabled
            and not self.get_missing_runtime_execution_settings()
        )


@lru_cache
def get_settings() -> Settings:
    """Cached singleton for application settings."""
    return Settings()
