"""Read-only Azure Data Factory discovery connector — Phase 9.

Production-safe, strictly read-only access to a single configured Azure
Data Factory. Design guarantees:

* Credentials come only from configuration (environment variables).
* Real discovery is disabled unless explicitly enabled AND fully
  configured (see Settings.azure_discovery_ready()).
* Every call is scoped to exactly the configured subscription,
  resource group, and Data Factory — a strict boundary that also
  re-validates every returned Azure resource id.
* Only list/get (read) operations exist. There are deliberately no
  create/update/publish/execute/delete/register methods.
* No credentials, tokens, or raw SDK error text ever reach logs or
  API responses — failures are mapped to stable, sanitized codes.

The Azure SDK clients are injected so unit tests can supply fakes and
never touch the network.
"""

import logging
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def _rest_key_transformer():
    """Return a REST (camelCase) key transformer, or None if unavailable.

    Track2 azure-mgmt SDKs vendor msrest serialization; fall back to a
    real msrest install if present. Returning None means callers use the
    SDK's default as_dict() keys.
    """
    for module in (
        "azure.mgmt.datafactory._utils.serialization",
        "azure.mgmt.datafactory._serialization",
        "msrest.serialization",
    ):
        try:
            mod = __import__(module, fromlist=["full_restapi_key_transformer"])
            return getattr(mod, "full_restapi_key_transformer")
        except Exception:
            continue
    return None


# Error codes surfaced to callers (never include raw SDK detail).
CODE_DISABLED = "AZURE_DISCOVERY_DISABLED"
CODE_CONFIG = "AZURE_CONFIG_INCOMPLETE"
CODE_AUTH = "AZURE_AUTH_FAILED"
CODE_AUTHZ = "AZURE_AUTHORIZATION_FAILED"
CODE_NOT_FOUND = "AZURE_NOT_FOUND"
CODE_TIMEOUT = "AZURE_TIMEOUT"
CODE_BOUNDARY = "AZURE_BOUNDARY_VIOLATION"
CODE_MALFORMED = "AZURE_MALFORMED_RESPONSE"
CODE_UNKNOWN = "AZURE_DISCOVERY_ERROR"


class AzureDiscoveryError(Exception):
    """Sanitized, read-only Azure discovery failure with a stable code."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code


def _default_credential_factory(tenant_id, client_id, client_secret):
    from azure.identity import ClientSecretCredential

    return ClientSecretCredential(
        tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
    )


def _default_datafactory_factory(credential, subscription_id):
    from azure.mgmt.datafactory import DataFactoryManagementClient

    return DataFactoryManagementClient(credential, subscription_id)


def _default_resource_factory(credential, subscription_id):
    from azure.mgmt.resource import ResourceManagementClient

    return ResourceManagementClient(credential, subscription_id)


class AzureADFClient:
    """Strictly read-only client for one configured Data Factory."""

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        subscription_id: str,
        resource_group: str,
        data_factory_name: str,
        timeout_seconds: int = 60,
        credential_factory: Optional[Callable] = None,
        datafactory_client_factory: Optional[Callable] = None,
        resource_client_factory: Optional[Callable] = None,
    ):
        missing = [
            n
            for n, v in {
                "tenant_id": tenant_id,
                "client_id": client_id,
                "client_secret": client_secret,
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "data_factory_name": data_factory_name,
            }.items()
            if not v
        ]
        if missing:
            raise AzureDiscoveryError(
                f"Azure discovery configuration incomplete: {missing}",
                CODE_CONFIG,
            )

        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret  # never logged
        self.subscription_id = subscription_id
        self.resource_group = resource_group
        self.data_factory_name = data_factory_name
        self.timeout_seconds = timeout_seconds

        self._credential_factory = credential_factory or _default_credential_factory
        self._datafactory_factory = (
            datafactory_client_factory or _default_datafactory_factory
        )
        self._resource_factory = resource_client_factory or _default_resource_factory

        self._credential = None
        self._df_client = None
        self._rm_client = None

    # ── Lazy SDK clients ─────────────────────────────────────────

    def _credentials(self):
        if self._credential is None:
            self._credential = self._credential_factory(
                self._tenant_id, self._client_id, self._client_secret
            )
        return self._credential

    def _datafactory(self):
        if self._df_client is None:
            self._df_client = self._datafactory_factory(
                self._credentials(), self.subscription_id
            )
        return self._df_client

    def _resources(self):
        if self._rm_client is None:
            self._rm_client = self._resource_factory(
                self._credentials(), self.subscription_id
            )
        return self._rm_client

    # ── Error mapping (sanitized) ────────────────────────────────

    def _call(self, op_name: str, fn: Callable):
        """Run an SDK read call, mapping failures to sanitized errors."""
        try:
            return fn()
        except AzureDiscoveryError:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberately broad, sanitized
            code = self._classify(exc)
            # Log type + code only; never the message (may hold detail).
            logger.warning(
                "Azure read '%s' failed [%s] (%s)",
                op_name, code, type(exc).__name__,
            )
            raise AzureDiscoveryError(
                f"Azure read operation '{op_name}' failed.", code
            ) from None

    @staticmethod
    def _classify(exc: Exception) -> str:
        status = getattr(exc, "status_code", None)
        name = type(exc).__name__
        if name == "ClientAuthenticationError" or status in (401,):
            return CODE_AUTH
        if status == 403:
            return CODE_AUTHZ
        if name == "ResourceNotFoundError" or status == 404:
            return CODE_NOT_FOUND
        if name in ("ServiceRequestError", "ServiceResponseTimeoutError") or isinstance(
            exc, TimeoutError
        ):
            return CODE_TIMEOUT
        return CODE_UNKNOWN

    # ── Boundary enforcement ─────────────────────────────────────

    def _assert_within_boundary(self, resource_id: Optional[str]) -> None:
        """Reject any resource id outside the configured sub + RG."""
        if not resource_id:
            return
        rid = resource_id.lower()
        expected = (
            f"/subscriptions/{self.subscription_id}/resourcegroups/"
            f"{self.resource_group}".lower()
        )
        # Accept the resource group's own id (exact) or any child (prefix/).
        if not (rid == expected or rid.startswith(expected + "/")):
            raise AzureDiscoveryError(
                "Azure returned a resource outside the configured boundary.",
                CODE_BOUNDARY,
            )

    # ── Serialization (lossless, sanitized) ──────────────────────

    @staticmethod
    def _to_definition(item: Any) -> dict:
        """Return an item's complete definition dict without dropping props."""
        try:
            if hasattr(item, "as_dict"):
                # Prefer REST (camelCase) keys so downstream alias-based
                # models and expression/connection extraction match real
                # Azure JSON. Fakes (and any client without the transformer)
                # fall back to a plain as_dict().
                transformer = _rest_key_transformer()
                try:
                    data = (
                        item.as_dict(key_transformer=transformer)
                        if transformer is not None
                        else item.as_dict()
                    )
                except Exception:
                    data = item.as_dict()
            elif isinstance(item, dict):
                data = item
            else:
                raise AzureDiscoveryError(
                    "Azure item is not serializable.", CODE_MALFORMED
                )
        except AzureDiscoveryError:
            raise
        except Exception:
            raise AzureDiscoveryError(
                "Azure item could not be serialized.", CODE_MALFORMED
            ) from None
        if not isinstance(data, dict) or "name" not in data:
            raise AzureDiscoveryError(
                "Azure item is missing required fields.", CODE_MALFORMED
            )
        return data

    # ── Verification (read-only) ─────────────────────────────────

    def verify_environment(self) -> dict:
        """Confirm the subscription is reachable and the RG boundary holds."""
        rm = self._resources()
        group = self._call(
            "resource_groups.get",
            lambda: rm.resource_groups.get(self.resource_group),
        )
        self._assert_within_boundary(getattr(group, "id", None))
        return {
            "subscription_accessible": True,
            "resource_group": self.resource_group,
            "resource_group_location": getattr(group, "location", None),
            "boundary_enforced": True,
        }

    def verify_data_factory(self) -> dict:
        """Confirm the configured Data Factory exists in the exact RG."""
        df = self._datafactory()
        factory = self._call(
            "factories.get",
            lambda: df.factories.get(self.resource_group, self.data_factory_name),
        )
        self._assert_within_boundary(getattr(factory, "id", None))
        return {
            "name": getattr(factory, "name", self.data_factory_name),
            "location": getattr(factory, "location", None),
            "provisioning_state": getattr(
                getattr(factory, "provisioning_state", None), "value",
                getattr(factory, "provisioning_state", None),
            ),
            "exists": True,
        }

    def provider_status(self, namespace: str) -> dict:
        """Return the registration state of a resource provider (read-only)."""
        if namespace not in ("Microsoft.DataFactory", "Microsoft.Storage"):
            raise AzureDiscoveryError(
                "Only Microsoft.DataFactory and Microsoft.Storage are checked.",
                CODE_BOUNDARY,
            )
        rm = self._resources()
        provider = self._call(
            "providers.get", lambda: rm.providers.get(namespace)
        )
        return {
            "namespace": getattr(provider, "namespace", namespace),
            "registration_state": getattr(provider, "registration_state", None),
        }

    # ── Read-only listing + full definitions ─────────────────────

    def _list_with_definitions(self, op_name: str, list_fn, get_fn) -> list[dict]:
        items = self._call(op_name, lambda: list(list_fn()))
        definitions: list[dict] = []
        for item in items:
            name = getattr(item, "name", None) or (
                item.get("name") if isinstance(item, dict) else None
            )
            if not name:
                raise AzureDiscoveryError(
                    "Azure list item is missing a name.", CODE_MALFORMED
                )
            full = self._call(f"{op_name}.get", lambda n=name: get_fn(n))
            definition = self._to_definition(full)
            self._assert_within_boundary(definition.get("id"))
            definitions.append(definition)
        return definitions

    def list_pipelines(self) -> list[dict]:
        c = self._datafactory()
        return self._list_with_definitions(
            "pipelines",
            lambda: c.pipelines.list_by_factory(
                self.resource_group, self.data_factory_name
            ),
            lambda n: c.pipelines.get(
                self.resource_group, self.data_factory_name, n
            ),
        )

    def list_data_flows(self) -> list[dict]:
        c = self._datafactory()
        return self._list_with_definitions(
            "data_flows",
            lambda: c.data_flows.list_by_factory(
                self.resource_group, self.data_factory_name
            ),
            lambda n: c.data_flows.get(
                self.resource_group, self.data_factory_name, n
            ),
        )

    def list_datasets(self) -> list[dict]:
        c = self._datafactory()
        return self._list_with_definitions(
            "datasets",
            lambda: c.datasets.list_by_factory(
                self.resource_group, self.data_factory_name
            ),
            lambda n: c.datasets.get(
                self.resource_group, self.data_factory_name, n
            ),
        )

    def list_linked_services(self) -> list[dict]:
        c = self._datafactory()
        return self._list_with_definitions(
            "linked_services",
            lambda: c.linked_services.list_by_factory(
                self.resource_group, self.data_factory_name
            ),
            lambda n: c.linked_services.get(
                self.resource_group, self.data_factory_name, n
            ),
        )

    def list_triggers(self) -> list[dict]:
        c = self._datafactory()
        return self._list_with_definitions(
            "triggers",
            lambda: c.triggers.list_by_factory(
                self.resource_group, self.data_factory_name
            ),
            lambda n: c.triggers.get(
                self.resource_group, self.data_factory_name, n
            ),
        )

    def discover_raw(self) -> dict:
        """Return complete raw definitions for every supported asset type."""
        return {
            "linked_services": self.list_linked_services(),
            "datasets": self.list_datasets(),
            "data_flows": self.list_data_flows(),
            "pipelines": self.list_pipelines(),
            "triggers": self.list_triggers(),
        }


# Regex kept module-level for potential reuse by conversion layer.
_RESOURCE_ID_RE = re.compile(
    r"^/subscriptions/(?P<sub>[^/]+)/resourcegroups/(?P<rg>[^/]+)/", re.IGNORECASE
)
