"""In-memory Microsoft Fabric definition deployment simulator.

Makes no network calls, stores no credentials, and deliberately exposes no
destructive methods. The deployment engine uses ``deploy_artifact``; legacy
name-only helpers remain solely for backward-compatible callers/tests.
"""

import hashlib
import logging
from copy import deepcopy
from typing import Optional

from src.artifacts import validate_generated_artifact
from src.models.schemas import DeployableTargetType, GeneratedArtifact

logger = logging.getLogger(__name__)

CREATION_KINDS = (
    "connection", "lakehouse", "table", "dataflow", "pipeline", "schedule"
)

_ACTION_FOR_TARGET = {
    DeployableTargetType.CONNECTION: "create_connection",
    DeployableTargetType.LAKEHOUSE: "create_lakehouse",
    DeployableTargetType.LAKEHOUSE_TABLE: "create_table",
    DeployableTargetType.DATAFLOW_GEN2: "create_dataflow",
    DeployableTargetType.DATA_PIPELINE: "create_pipeline",
    DeployableTargetType.SCHEDULE: "configure_schedule",
}
_KIND_FOR_TARGET = {
    DeployableTargetType.CONNECTION: "connection",
    DeployableTargetType.LAKEHOUSE: "lakehouse",
    DeployableTargetType.LAKEHOUSE_TABLE: "table",
    DeployableTargetType.DATAFLOW_GEN2: "dataflow",
    DeployableTargetType.DATA_PIPELINE: "pipeline",
    DeployableTargetType.SCHEDULE: "schedule",
}


class MockFabricError(Exception):
    """Raised to report an invalid definition or injected mock failure."""


class MockFabricClient:
    """Definition-aware in-memory stand-in for Microsoft Fabric."""

    def __init__(self, fail_on_action: Optional[str] = None):
        self._resources: dict[str, dict] = {}
        self._legacy_resources: dict[tuple, str] = {}
        self._workspaces: dict[str, str] = {}
        self._runs: dict[str, str] = {}
        self.fail_on_action = fail_on_action

    @staticmethod
    def _mock_id(kind: str, name: str) -> str:
        return f"mock-{kind}-{name}"

    def _maybe_fail(self, action_key: str) -> None:
        if self.fail_on_action == action_key:
            raise MockFabricError(f"Injected failure on '{action_key}'.")

    def _create(self, kind: str, name: str, action_key: str) -> str:
        self._maybe_fail(action_key)
        key = (kind, name)
        if key in self._legacy_resources:
            return self._legacy_resources[key]
        mock_id = self._mock_id(kind, name)
        self._legacy_resources[key] = mock_id
        return mock_id

    def deploy_artifact(self, artifact: GeneratedArtifact) -> str:
        """Validate and idempotently deploy a generated definition."""
        action_key = _ACTION_FOR_TARGET[artifact.target_type]
        if self.fail_on_action in (action_key, artifact.artifact_id):
            raise MockFabricError(
                f"Injected failure on '{self.fail_on_action}'."
            )
        schema = validate_generated_artifact(artifact)
        if not schema.valid:
            raise MockFabricError(
                f"Definition schema invalid for {artifact.artifact_id}: "
                + "; ".join(schema.errors)
            )

        existing = self._resources.get(artifact.artifact_id)
        if existing is not None:
            if existing["content_digest"] != artifact.content_digest:
                raise MockFabricError(
                    f"Artifact {artifact.artifact_id} already exists with "
                    "different content."
                )
            return existing["resource_id"]

        identity = f"{artifact.artifact_id}|{artifact.content_digest}"
        suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        kind = _KIND_FOR_TARGET[artifact.target_type]
        resource_id = f"mock-{kind}-{suffix}"
        self._resources[artifact.artifact_id] = {
            "artifact_id": artifact.artifact_id,
            "target_type": artifact.target_type.value,
            "target_name": artifact.target_name,
            "content_digest": artifact.content_digest,
            "generated_definition": deepcopy(artifact.generated_definition),
            "resource_id": resource_id,
        }
        logger.info(
            "Mock deployed %s '%s' -> %s",
            artifact.target_type.value,
            artifact.target_name,
            resource_id,
        )
        return resource_id

    # Compatibility helpers. DeploymentService does not use these.
    def verify_workspace(self, name: str) -> str:
        self._maybe_fail("verify_workspace")
        mock_id = self._mock_id("workspace", name)
        self._workspaces[name] = mock_id
        return mock_id

    def create_connection(self, name: str) -> str:
        return self._create("connection", name, "create_connection")

    def create_lakehouse(self, name: str) -> str:
        return self._create("lakehouse", name, "create_lakehouse")

    def create_table(self, name: str) -> str:
        return self._create("table", name, "create_table")

    def create_dataflow(self, name: str) -> str:
        return self._create("dataflow", name, "create_dataflow")

    def create_pipeline(self, name: str) -> str:
        return self._create("pipeline", name, "create_pipeline")

    def configure_schedule(self, name: str) -> str:
        return self._create("schedule", name, "configure_schedule")

    def run_target(self, name: str) -> str:
        self._maybe_fail("run_target")
        run_id = self._mock_id("run", name)
        self._runs[name] = run_id
        return run_id

    def resource_count(self) -> int:
        return len(self._resources) + len(self._legacy_resources)

    def has_resource(self, kind: str, name: str) -> bool:
        if (kind, name) in self._legacy_resources:
            return True
        return any(
            item["target_name"] == name
            and _KIND_FOR_TARGET[DeployableTargetType(item["target_type"])] == kind
            for item in self._resources.values()
        )

    def get_deployed_artifact(self, artifact_id: str) -> Optional[dict]:
        item = self._resources.get(artifact_id)
        return deepcopy(item) if item is not None else None

    def deployed_artifacts(self) -> list[dict]:
        return [deepcopy(self._resources[key]) for key in sorted(self._resources)]
