"""Azure discovery source — Phase 9.

Adapts the read-only AzureADFClient to the shared ADFSource boundary by
converting complete Azure JSON definitions into the existing internal
ADF models (ADFInventory). Conversion is lossless: the ADF models allow
extra fields and camel/snake aliases, so no source property is dropped.
"""

import logging
import re
from typing import Any, Optional

from src.connectors.adf_source import ADFSource
from src.connectors.azure_adf_client import (
    CODE_DISABLED,
    CODE_MALFORMED,
    AzureADFClient,
    AzureDiscoveryError,
)
from src.models.schemas import (
    ADFInventory,
    ADFPipeline,
    Dataset,
    LinkedService,
    MappingDataFlow,
    Trigger,
)

logger = logging.getLogger(__name__)

REDACTED = "***REDACTED***"
# Keys whose string values are secret material and must never be persisted.
_SENSITIVE_KEY_RE = re.compile(
    r"(password|secret|token|accountkey|account_key|connectionstring|"
    r"connection_string|serviceprincipalkey|service_principal_key|sastoken|"
    r"sas_token|accesskey|access_key|credential|apikey|api_key|authkey)",
    re.IGNORECASE,
)


def redact_secret_values(obj: Any) -> Any:
    """Redact secret string values while preserving keys and structure.

    Dict values (e.g. Key Vault references) are recursed into, not
    dropped, so definitions stay structurally lossless — only literal
    secret strings become ``***REDACTED***``.
    """
    if isinstance(obj, dict):
        redacted = {}
        for key, value in obj.items():
            if isinstance(value, str) and value and _SENSITIVE_KEY_RE.search(key):
                redacted[key] = REDACTED
            else:
                redacted[key] = redact_secret_values(value)
        return redacted
    if isinstance(obj, list):
        return [redact_secret_values(item) for item in obj]
    return obj


def convert_raw_to_inventory(raw: dict) -> ADFInventory:
    """Convert complete raw Azure definitions into an ADFInventory.

    Raises AzureDiscoveryError(CODE_MALFORMED) on invalid/incomplete
    definitions rather than leaking a raw validation error.
    """
    linked_services_data = raw.get("linked_services", [])
    datasets_data = raw.get("datasets", [])
    data_flows_data = raw.get("data_flows", [])
    pipelines_data = raw.get("pipelines", [])
    triggers_data = raw.get("triggers", [])
    try:
        return ADFInventory(
            linked_services=[LinkedService(**d) for d in linked_services_data],
            datasets=[Dataset(**d) for d in datasets_data],
            data_flows=[MappingDataFlow(**d) for d in data_flows_data],
            pipelines=[ADFPipeline(**d) for d in pipelines_data],
            triggers=[Trigger(**d) for d in triggers_data],
            # Preserve the complete original definitions losslessly so
            # downstream expression / connection extraction works exactly
            # as it does for fixture discovery.
            source_definitions={
                "pipelines": pipelines_data,
                "linked_services": linked_services_data,
                "datasets": datasets_data,
                "data_flows": data_flows_data,
                "triggers": triggers_data,
            },
        )
    except AzureDiscoveryError:
        raise
    except Exception:
        raise AzureDiscoveryError(
            "Azure definition could not be converted to the internal model.",
            CODE_MALFORMED,
        ) from None


class AzureADFSource(ADFSource):
    """Read-only Azure Data Factory discovery source."""

    mode = "azure"

    def __init__(self, client: AzureADFClient):
        self.client = client

    def load_inventory(self) -> ADFInventory:
        raw = self.client.discover_raw()
        # Redact secret values before anything else touches the data, so
        # secrets never reach memory downstream, persistence, or logs.
        raw = {
            key: [redact_secret_values(d) for d in defs]
            for key, defs in raw.items()
        }
        inventory = convert_raw_to_inventory(raw)
        logger.info(
            "Azure discovery loaded: %d pipelines, %d data flows, %d datasets, "
            "%d linked services, %d triggers.",
            len(inventory.pipelines),
            len(inventory.data_flows),
            len(inventory.datasets),
            len(inventory.linked_services),
            len(inventory.triggers),
        )
        return inventory

    def describe(self) -> dict:
        return {
            "mode": self.mode,
            "resource_group": self.client.resource_group,
            "data_factory_name": self.client.data_factory_name,
        }


def build_azure_adf_client_from_settings(settings) -> AzureADFClient:
    """Build an AzureADFClient from settings, enforcing the enable gate."""
    if not settings.enable_azure_discovery:
        raise AzureDiscoveryError(
            "Azure discovery is disabled (set ENABLE_AZURE_DISCOVERY=true).",
            CODE_DISABLED,
        )
    return AzureADFClient(
        tenant_id=settings.azure_tenant_id,
        client_id=settings.azure_client_id,
        client_secret=settings.azure_client_secret,
        subscription_id=settings.azure_subscription_id,
        resource_group=settings.azure_resource_group,
        data_factory_name=settings.azure_data_factory_name,
        timeout_seconds=settings.azure_discovery_timeout_seconds,
    )


def build_azure_source_from_settings(
    settings, client: Optional[AzureADFClient] = None
) -> AzureADFSource:
    """Build an AzureADFSource from settings (or wrap an injected client)."""
    return AzureADFSource(client or build_azure_adf_client_from_settings(settings))
