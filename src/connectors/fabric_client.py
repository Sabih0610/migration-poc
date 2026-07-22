"""Read/write Microsoft Fabric connector — Phase 10.

Production-safe client for one configured Fabric workspace. It uses the
Fabric REST API with Microsoft Entra service-principal auth over an
*injectable* HTTP transport so unit tests never contact real Fabric.

Guarantees:

* Real deployment is disabled unless explicitly enabled AND configured
  (Settings.fabric_deployment_ready()).
* Every request is scoped to exactly the configured workspace id; any
  item belonging to a different workspace is rejected.
* Only read + create/reuse operations exist. There are deliberately no
  delete/remove/drop methods (a test asserts this).
* The client consumes approved *generated definitions* — it never
  invents resources from action names — and validates each definition
  before any call.
* No credentials, tokens, or raw error bodies reach logs, responses,
  reports, or artifacts — failures map to stable, sanitized codes.
"""

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from src.artifacts import validate_generated_artifact
from src.connectors import fabric_definition_adapter as adapter
from src.models.schemas import DeployableTargetType, GeneratedArtifact

logger = logging.getLogger(__name__)

# Sanitized error codes (never carry raw SDK/HTTP detail).
CODE_DISABLED = "FABRIC_DEPLOYMENT_DISABLED"
CODE_CONFIG = "FABRIC_CONFIG_INCOMPLETE"
CODE_AUTH = "FABRIC_AUTH_FAILED"
CODE_AUTHZ = "FABRIC_AUTHORIZATION_FAILED"
CODE_NOT_FOUND = "FABRIC_NOT_FOUND"
CODE_CONFLICT = "FABRIC_CONFLICT"
CODE_THROTTLED = "FABRIC_THROTTLED"
CODE_TIMEOUT = "FABRIC_TIMEOUT"
CODE_BOUNDARY = "FABRIC_BOUNDARY_VIOLATION"
CODE_SCHEMA = "FABRIC_SCHEMA_INVALID"
CODE_NON_DEPLOYABLE = "FABRIC_ARTIFACT_NON_DEPLOYABLE"
CODE_ERROR = "FABRIC_ERROR"

# DeployableTargetType -> Fabric workspace item "type". Only item types that
# are real Fabric *workspace items* with a public-definition adapter belong
# here (Connection/LakehouseTable/Schedule are NOT workspace items — see
# _deploy_connection / _deploy_table / _deploy_schedule).
_ITEM_TYPE = {
    DeployableTargetType.LAKEHOUSE: "Lakehouse",
    DeployableTargetType.DATAFLOW_GEN2: "DataflowGen2",
    DeployableTargetType.DATA_PIPELINE: "DataPipeline",
}

# Item types Fabric supports a getDefinition read-back for (used to verify
# what was actually stored matches what we approved/sent).
_READBACK_SUPPORTED = {"DataPipeline", "DataflowGen2"}


class FabricError(Exception):
    """Sanitized Fabric failure with a stable code."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class FabricItemOutcome:
    """Result of deploying one generated artifact to Fabric."""

    artifact_id: str
    target_name: str
    item_type: str
    status: str  # "created" | "reused" | "deferred" | "failed"
    item_id: Optional[str] = None
    content_digest: Optional[str] = None
    reused: bool = False
    error: Optional[str] = None
    error_code: Optional[str] = None
    # Set for LakehouseTable: table materialization never runs in Phase 10.
    materialization_status: Optional[str] = None
    # Set for item types with a supported getDefinition read-back
    # ("MATCH" | "MISMATCH" | "UNSUPPORTED").
    readback_status: Optional[str] = None
    readback_digest: Optional[str] = None


@dataclass
class _Response:
    status_code: int
    _json: Any = None
    headers: dict = field(default_factory=dict)

    def json(self):
        return self._json


def _default_token_provider(tenant_id, client_id, client_secret, scope):
    def _get() -> str:
        from azure.identity import ClientSecretCredential

        cred = ClientSecretCredential(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
        )
        return cred.get_token(scope).token

    return _get


def _default_transport(timeout_seconds):
    import httpx

    client = httpx.Client(timeout=timeout_seconds)

    def _request(method, url, headers=None, json_body=None):
        resp = client.request(method, url, headers=headers, json=json_body)
        try:
            parsed = resp.json()
        except Exception:
            parsed = None
        return _Response(resp.status_code, parsed, dict(resp.headers))

    return _request


class FabricClient:
    """Workspace-scoped Fabric REST client (read + create/reuse only)."""

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        workspace_id: str,
        capacity_id: str = "",
        base_url: str = "https://api.fabric.microsoft.com/v1",
        scope: str = "https://api.fabric.microsoft.com/.default",
        timeout_seconds: int = 120,
        max_retries: int = 4,
        token_provider: Optional[Callable[[], str]] = None,
        transport: Optional[Callable] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        missing = [
            n
            for n, v in {
                "tenant_id": tenant_id,
                "client_id": client_id,
                "client_secret": client_secret,
                "workspace_id": workspace_id,
            }.items()
            if not v
        ]
        if missing:
            raise FabricError(
                f"Fabric configuration incomplete: {missing}", CODE_CONFIG
            )

        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret  # never logged
        self.workspace_id = workspace_id
        self.capacity_id = capacity_id
        self.base_url = base_url.rstrip("/")
        self.scope = scope
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._sleep = sleep_fn

        self._token_provider = token_provider or _default_token_provider(
            tenant_id, client_id, client_secret, scope
        )
        self._transport = transport or _default_transport(timeout_seconds)
        self._token: Optional[str] = None

    # ── Auth ─────────────────────────────────────────────────────

    def _bearer(self) -> str:
        if self._token is None:
            try:
                self._token = self._token_provider()
            except Exception:
                raise FabricError(
                    "Fabric authentication failed.", CODE_AUTH
                ) from None
            if not self._token:
                raise FabricError("Fabric authentication failed.", CODE_AUTH)
        return self._token

    # ── HTTP with retry / throttle / timeout / sanitized errors ──

    def _request(self, method: str, path: str, body: Any = None) -> _Response:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._bearer()}",  # never logged
            "Content-Type": "application/json",
        }
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._transport(method, url, headers=headers, json_body=body)
            except Exception as exc:  # transport-level (network/timeout)
                if _is_timeout(exc) and attempt <= self.max_retries:
                    self._sleep(_backoff(attempt))
                    continue
                code = CODE_TIMEOUT if _is_timeout(exc) else CODE_ERROR
                logger.warning("Fabric %s failed [%s] (%s)", path, code, type(exc).__name__)
                raise FabricError(f"Fabric request to '{path}' failed.", code) from None

            status = resp.status_code
            if status in (429, 503) and attempt <= self.max_retries:
                self._sleep(_retry_after(resp, attempt))
                continue
            if 200 <= status < 300:
                return resp
            raise self._http_error(path, status)

    def _http_error(self, path: str, status: int) -> FabricError:
        code = {
            401: CODE_AUTH,
            403: CODE_AUTHZ,
            404: CODE_NOT_FOUND,
            409: CODE_CONFLICT,
            429: CODE_THROTTLED,
            503: CODE_THROTTLED,
        }.get(status, CODE_ERROR)
        logger.warning("Fabric %s -> HTTP %s [%s]", path, status, code)
        # Never surface the response body (may echo request context).
        return FabricError(f"Fabric request to '{path}' failed (HTTP {status}).", code)

    # ── Boundary ─────────────────────────────────────────────────

    def _ws(self, suffix: str = "") -> str:
        return f"/workspaces/{self.workspace_id}{suffix}"

    def _assert_workspace(self, item: dict) -> None:
        ws = item.get("workspaceId")
        if ws and ws != self.workspace_id:
            raise FabricError(
                "Fabric returned an item outside the configured workspace.",
                CODE_BOUNDARY,
            )

    # ── Read-only verification ───────────────────────────────────

    def verify_authentication(self) -> dict:
        self._bearer()
        return {"authenticated": True}

    def verify_workspace(self) -> dict:
        resp = self._request("GET", self._ws())
        data = resp.json() or {}
        self._assert_workspace({"workspaceId": data.get("id", self.workspace_id)})
        if data.get("id") and data["id"] != self.workspace_id:
            raise FabricError("Workspace id mismatch.", CODE_BOUNDARY)
        return {
            "workspace_id": self.workspace_id,
            "display_name": data.get("displayName"),
            "capacity_id": data.get("capacityId"),
            "accessible": True,
        }

    def verify_capacity(self) -> dict:
        resp = self._request("GET", self._ws())
        data = resp.json() or {}
        assigned = data.get("capacityId")
        result = {"assigned_capacity_id": assigned, "state": None, "matches_config": None}
        if assigned:
            try:
                cap = self._request("GET", f"/capacities/{assigned}").json() or {}
                result["state"] = cap.get("state")
            except FabricError as exc:
                # A service principal frequently lacks tenant-level rights to
                # read capacity state. That is NOT the same as the capacity
                # being unassigned/inactive — report explicitly so callers
                # never invent a status.
                if exc.code == CODE_AUTHZ:
                    result["state"] = "CAPACITY_STATE_NOT_VERIFIABLE"
                else:
                    raise
        if self.capacity_id:
            result["matches_config"] = assigned == self.capacity_id
        return result

    def verify_permissions(self) -> dict:
        resp = self._request("GET", self._ws("/roleAssignments"))
        data = resp.json() or {}
        roles = [
            a.get("role")
            for a in data.get("value", [])
            if a.get("principal", {}).get("id") == self._client_id
        ]
        return {"principal_roles": roles, "has_role": bool(roles)}

    def list_items(self, item_type: Optional[str] = None) -> list[dict]:
        resp = self._request("GET", self._ws("/items"))
        items = (resp.json() or {}).get("value", [])
        for item in items:
            self._assert_workspace(item)
        if item_type:
            items = [i for i in items if i.get("type") == item_type]
        return items

    def verify_environment(self) -> dict:
        return {
            "authentication": self.verify_authentication(),
            "workspace": self.verify_workspace(),
            "capacity": self.verify_capacity(),
            "permissions": self.verify_permissions(),
            "item_count": len(self.list_items()),
            "tenant_settings": self._tenant_settings_readiness(),
        }

    def _tenant_settings_readiness(self) -> dict:
        # Tenant admin settings are frequently not readable by a workspace
        # service principal; report best-effort rather than failing.
        try:
            resp = self._request("GET", "/admin/tenantsettings")
            settings = (resp.json() or {}).get("tenantSettings", [])
            spn_enabled = any(
                s.get("settingName", "").lower().startswith("serviceprincipal")
                and s.get("enabled")
                for s in settings
            )
            return {"readable": True, "service_principal_enabled": spn_enabled}
        except FabricError:
            return {"readable": False, "service_principal_enabled": None}

    # ── Create-or-reuse (consumes approved generated definitions) ─

    def deploy_artifact(
        self, artifact: GeneratedArtifact, dependency_ids: Optional[dict] = None
    ) -> FabricItemOutcome:
        """Create or reuse the Fabric item for one generated artifact."""
        schema = validate_generated_artifact(artifact)
        if not schema.valid:
            raise FabricError(
                f"Definition schema invalid for {artifact.artifact_id}: "
                + "; ".join(schema.errors),
                CODE_SCHEMA,
            )
        target = artifact.target_type
        if target in _ITEM_TYPE:
            return self._deploy_definition_item(artifact, _ITEM_TYPE[target])
        if target == DeployableTargetType.CONNECTION:
            return self._deploy_connection(artifact)
        if target == DeployableTargetType.LAKEHOUSE_TABLE:
            return self._deploy_table(artifact, dependency_ids or {})
        if target == DeployableTargetType.SCHEDULE:
            return self._deploy_schedule(artifact, dependency_ids or {})
        raise FabricError(
            f"Unsupported target type '{target}'.", CODE_ERROR
        )

    def _find_item(self, item_type: str, display_name: str) -> Optional[dict]:
        for item in self.list_items(item_type):
            if item.get("displayName") == display_name:
                return item
        return None

    def _outcome(
        self,
        artifact,
        item_type,
        item_id,
        reused,
        status: Optional[str] = None,
        materialization_status: Optional[str] = None,
        readback_status: Optional[str] = None,
        readback_digest: Optional[str] = None,
    ) -> FabricItemOutcome:
        return FabricItemOutcome(
            artifact_id=artifact.artifact_id,
            target_name=artifact.target_name,
            item_type=item_type,
            status=status or ("reused" if reused else "created"),
            item_id=item_id,
            content_digest=artifact.content_digest,
            reused=reused,
            materialization_status=materialization_status,
            readback_status=readback_status,
            readback_digest=readback_digest,
        )

    def _create_item(self, item_type: str, body: dict, path: str) -> str:
        """POST an item and resolve its id, handling 202 long-running ops."""
        resp = self._request("POST", path, body)
        if resp.status_code == 202:
            location = resp.headers.get("location") or resp.headers.get("Location")
            item_id = self._await_operation(location)
            if item_id:
                return item_id
        data = resp.json() or {}
        item_id = data.get("id")
        if not item_id:
            raise FabricError("Fabric did not return an item id.", CODE_ERROR)
        return item_id

    def _poll_operation(self, location: Optional[str]) -> dict:
        """Poll a long-running-operation location to completion; return its
        final result JSON (caller extracts whatever field it needs)."""
        if not location:
            return {}
        path = location.replace(self.base_url, "") if location.startswith(self.base_url) else location
        for attempt in range(1, self.max_retries + 2):
            data = (self._request("GET", path).json()) or {}
            status = str(data.get("status", "")).lower()
            if status in ("succeeded", "completed"):
                return self._request("GET", f"{path}/result").json() or data
            if status in ("failed", "cancelled"):
                raise FabricError("Fabric operation failed.", CODE_ERROR)
            self._sleep(_backoff(attempt))
        raise FabricError("Fabric operation timed out.", CODE_TIMEOUT)

    def _await_operation(self, location: Optional[str]) -> Optional[str]:
        result = self._poll_operation(location)
        return result.get("id")

    # ── Lakehouse / Dataflow Gen2 / Data Pipeline (adapter-backed) ─

    def _deploy_definition_item(self, artifact, item_type: str) -> FabricItemOutcome:
        built = adapter.build_definition(artifact)
        if not built.deployable:
            raise FabricError(
                f"Artifact {artifact.artifact_id} is NON_DEPLOYABLE: {built.reason}",
                CODE_NON_DEPLOYABLE,
            )
        approved_digest = adapter.definition_digest(built.parts)

        existing = self._find_item(item_type, artifact.target_name)
        if existing:
            self._assert_workspace(existing)
            item_id = existing.get("id")
            readback_status, readback_digest = self._readback(
                item_id, item_type, approved_digest
            )
            return self._outcome(
                artifact, item_type, item_id, reused=True,
                readback_status=readback_status, readback_digest=readback_digest,
            )

        body = {
            "displayName": artifact.target_name,
            "type": item_type,
            "definition": {"parts": built.parts},
        }
        item_id = self._create_item(item_type, body, self._ws("/items"))
        readback_status, readback_digest = self._readback(
            item_id, item_type, approved_digest
        )
        return self._outcome(
            artifact, item_type, item_id, reused=False,
            readback_status=readback_status, readback_digest=readback_digest,
        )

    def _readback(
        self, item_id: str, item_type: str, approved_digest: str
    ) -> tuple[str, Optional[str]]:
        """GET the definition Fabric actually stored and compare its digest
        against the approved definition we sent. Never treat the local POST
        alone as proof of deployment for supported types."""
        if item_type not in _READBACK_SUPPORTED:
            return "UNSUPPORTED", None
        resp = self._request("POST", self._ws(f"/items/{item_id}/getDefinition"))
        if resp.status_code == 202:
            location = resp.headers.get("location") or resp.headers.get("Location")
            result = self._poll_operation(location)
        else:
            result = resp.json() or {}
        definition = result.get("definition", {}) if isinstance(result, dict) else {}
        parts = definition.get("parts", []) if isinstance(definition, dict) else []
        actual_digest = adapter.definition_digest(parts) if parts else None
        if actual_digest is None:
            return "MISMATCH", None
        return ("MATCH" if actual_digest == approved_digest else "MISMATCH"), actual_digest

    # ── Connection (Connections API, never a workspace item) ───────

    def _find_connection(self, display_name: str) -> Optional[dict]:
        resp = self._request("GET", "/connections")
        items = (resp.json() or {}).get("value", [])
        for item in items:
            if item.get("displayName") == display_name:
                return item
        return None

    @staticmethod
    def _connection_secret_env_name(artifact_id: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9]+", "_", artifact_id).upper()
        return f"FABRIC_CONN_SECRET_{sanitized}"

    def _connection_credential_body(self, artifact) -> dict:
        properties = artifact.generated_definition.get("properties", {}) or {}
        auth = properties.get("authentication", {}) or {}
        kind = auth.get("kind", "ManagedIdentity")
        if kind == "ManagedIdentity":
            # No secret material is ever required or read for identity-based auth.
            return {"credentialType": "WorkspaceIdentity"}
        env_name = self._connection_secret_env_name(artifact.artifact_id)
        secret = os.environ.get(env_name)
        if not secret:
            raise FabricError(
                f"Missing runtime credential env var '{env_name}' for connection "
                f"'{artifact.target_name}'. Credentials are never read from the "
                "artifact, package, or plan.",
                CODE_CONFIG,
            )
        # `secret` is used only in this one outbound request body; it is never
        # logged, returned in FabricItemOutcome, or persisted anywhere.
        return {"credentialType": "Key", "credentials": secret}

    def _deploy_connection(self, artifact) -> FabricItemOutcome:
        # Connections are managed by the separate Fabric Connections API —
        # they are NOT workspace items and must never go through
        # /workspaces/{id}/items.
        properties = artifact.generated_definition.get("properties", {}) or {}
        existing = self._find_connection(artifact.target_name)
        if existing:
            return self._outcome(artifact, "Connection", existing.get("id"), reused=True)
        body = {
            "displayName": artifact.target_name,
            "connectivityType": "ShareableCloud",
            "connectionDetails": {
                "type": properties.get("connectionType", ""),
                "parameters": [
                    {"dataType": "Text", "name": "endpoint",
                     "value": properties.get("endpoint", "")},
                ],
            },
            "credentialDetails": self._connection_credential_body(artifact),
        }
        item_id = self._create_item("Connection", body, "/connections")
        return self._outcome(artifact, "Connection", item_id, reused=False)

    # ── LakehouseTable (never a standalone item; deferred to runtime) ─

    def _deploy_table(self, artifact, dependency_ids: dict) -> FabricItemOutcome:
        # There is no supported Fabric API to create/manage an individual
        # Lakehouse table as a workspace item, and Phase 10 deliberately
        # never executes SQL or loads data. The approved table schema/DDL is
        # kept as part of the approved artifact package; materialization is
        # deferred to a future runtime step (e.g. a pipeline Copy activity).
        # No network call is made for this artifact type.
        return FabricItemOutcome(
            artifact_id=artifact.artifact_id,
            target_name=artifact.target_name,
            item_type="LakehouseTable",
            status="deferred",
            item_id=None,
            content_digest=artifact.content_digest,
            reused=False,
            materialization_status="DEFERRED_TO_RUNTIME",
            readback_status="UNSUPPORTED",
        )

    # ── Schedule (Fabric Job Scheduler, attached to the pipeline item) ─

    def _deploy_schedule(self, artifact, dependency_ids: dict) -> FabricItemOutcome:
        if not artifact.dependencies:
            raise FabricError(
                f"Schedule '{artifact.target_name}' has no parent pipeline dependency.",
                CODE_CONFIG,
            )
        pipeline_artifact_id = artifact.dependencies[0]
        pipeline_item_id = dependency_ids.get(pipeline_artifact_id)
        if not pipeline_item_id:
            raise FabricError(
                f"Schedule '{artifact.target_name}' requires its parent Data "
                f"Pipeline ('{pipeline_artifact_id}') to already be deployed "
                "with a real item id.",
                CODE_CONFIG,
            )
        properties = artifact.generated_definition.get("properties", {}) or {}
        job_type = "Pipeline"
        path = self._ws(f"/items/{pipeline_item_id}/jobs/{job_type}/schedules")

        existing = None
        try:
            resp = self._request("GET", path)
            for sched in (resp.json() or {}).get("value", []):
                if sched.get("displayName") == artifact.target_name:
                    existing = sched
                    break
        except FabricError as exc:
            if exc.code != CODE_NOT_FOUND:
                raise
        if existing:
            return self._outcome(artifact, "FabricSchedule", existing.get("id"), reused=True)

        # Explicit approved-activation opt-in only; otherwise stays disabled.
        activation_approved = bool(properties.get("activationApproved", False))
        body = {
            "displayName": artifact.target_name,
            "enabled": activation_approved,
            "configuration": {
                "type": "Cron",
                "recurrence": properties.get("recurrence", {}),
                "parameters": properties.get("parameters", {}),
            },
        }
        item_id = self._create_item("FabricSchedule", body, path)
        return self._outcome(artifact, "FabricSchedule", item_id, reused=False)


# ── Helpers ──────────────────────────────────────────────────────


def _is_timeout(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    return "timeout" in name or isinstance(exc, TimeoutError)


def _backoff(attempt: int) -> float:
    return min(2.0 ** (attempt - 1), 30.0)


def _retry_after(resp: _Response, attempt: int) -> float:
    header = (resp.headers or {}).get("retry-after") or (resp.headers or {}).get("Retry-After")
    if header:
        try:
            return float(header)
        except (TypeError, ValueError):
            pass
    return _backoff(attempt)


def build_fabric_client_from_settings(settings, **overrides) -> FabricClient:
    """Build a FabricClient from settings, enforcing the enable gate."""
    if not settings.fabric_deployment_enabled:
        raise FabricError(
            "Fabric deployment is disabled (set FABRIC_DEPLOYMENT_ENABLED=true).",
            CODE_DISABLED,
        )
    params = dict(
        tenant_id=settings.fabric_tenant_id,
        client_id=settings.fabric_client_id,
        client_secret=settings.fabric_client_secret,
        workspace_id=settings.fabric_workspace_id,
        capacity_id=settings.fabric_capacity_id,
        base_url=settings.fabric_api_base_url,
        scope=settings.fabric_scope,
        timeout_seconds=settings.fabric_timeout_seconds,
    )
    params.update(overrides)
    return FabricClient(**params)
