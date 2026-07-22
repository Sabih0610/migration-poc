"""Package-aware Fabric deployment engine.

Deploys approved generated definitions in dependency order. DRY_RUN performs
schema/authorization checks but creates nothing; MOCK uses the in-memory
definition-aware connector; REAL uses the read/write Fabric connector and
only runs when Fabric deployment is explicitly enabled and configured.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from src.approvals.deployment_guard import (
    DeploymentAuthorizationError,
    validate_deployment_authorization,
)
from src.artifacts import ArtifactPackageError
from src.config import get_settings
from src.connectors.fabric_client import (
    FabricClient,
    FabricError,
    build_fabric_client_from_settings,
)
from src.connectors.mock_fabric_client import MockFabricClient, MockFabricError
from src.migration.deployment_store import save_deployment
from src.migration.plan_store import get_plan, verify_plan_package
from src.models.schemas import (
    DeploymentMode,
    DeploymentResult,
    DeploymentStatus,
    DeploymentStepResult,
    DeploymentStepStatus,
    DeploymentSummary,
    GeneratedArtifact,
)

logger = logging.getLogger(__name__)


class RealModeNotImplementedError(Exception):
    """Kept for backward compatibility; REAL mode is now implemented."""


class FabricDeploymentDisabledError(Exception):
    """Raised when REAL deployment is requested but not enabled/configured."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def artifact_dependency_order(
    artifacts: list[GeneratedArtifact],
) -> list[GeneratedArtifact]:
    """Return deterministic dependency-first order or raise on invalid graph."""
    by_id = {artifact.artifact_id: artifact for artifact in artifacts}
    if len(by_id) != len(artifacts):
        raise ValueError("duplicate artifact IDs in generated package")
    for artifact in artifacts:
        missing = set(artifact.dependencies) - set(by_id)
        if missing:
            raise ValueError(
                f"artifact {artifact.artifact_id} has missing dependencies: "
                f"{sorted(missing)}"
            )

    ordered: list[GeneratedArtifact] = []
    completed: set[str] = set()
    remaining = dict(by_id)
    while remaining:
        ready = sorted(
            artifact_id
            for artifact_id, artifact in remaining.items()
            if set(artifact.dependencies) <= completed
        )
        if not ready:
            raise ValueError("generated artifact dependency graph contains a cycle")
        for artifact_id in ready:
            artifact = remaining.pop(artifact_id)
            ordered.append(artifact)
            completed.add(artifact_id)
    return ordered


class DeploymentService:
    """Deploy approved generated definitions to a mock or real connector."""

    def __init__(
        self,
        connector: Optional[MockFabricClient] = None,
        fabric_client: Optional[FabricClient] = None,
    ):
        self._connector = connector
        self._fabric_client = fabric_client

    def _resolve_real_client(self) -> FabricClient:
        """Return the injected/real Fabric client or fail if not enabled."""
        if self._fabric_client is not None:
            return self._fabric_client
        settings = get_settings()
        if not settings.fabric_deployment_ready():
            raise FabricDeploymentDisabledError(
                "REAL deployment requires FABRIC_DEPLOYMENT_ENABLED=true and "
                "full Fabric configuration."
            )
        return build_fabric_client_from_settings(settings)

    def deploy(
        self, plan_id: int, approval_id: int, mode: DeploymentMode
    ) -> DeploymentResult:
        mode = DeploymentMode(mode)
        started = _now()

        # REAL client is resolved up front so a disabled/misconfigured
        # environment fails before any authorization or package work.
        real_client = None
        if mode == DeploymentMode.REAL:
            real_client = self._resolve_real_client()  # raises if disabled

        try:
            authorization = validate_deployment_authorization(
                plan_id, approval_id
            )
        except DeploymentAuthorizationError as exc:
            return self._finish(
                plan_id,
                approval_id,
                mode,
                DeploymentStatus.BLOCKED,
                [],
                started,
                error=f"{exc.code}: {exc.message}",
            )

        plan = get_plan(plan_id)["plan"]
        package = plan.generated_package

        # For REAL, re-read + verify the persisted package immediately
        # before deployment: reject missing/modified/unexpected files.
        if mode == DeploymentMode.REAL:
            try:
                verify_plan_package(plan)
            except (ArtifactPackageError, Exception) as exc:
                return self._finish(
                    plan_id, approval_id, mode, DeploymentStatus.BLOCKED, [],
                    started, error=f"PACKAGE_VERIFICATION_FAILED: {exc}",
                    package_id=package.package_id if package else None,
                    plan_fingerprint=authorization.plan_fingerprint,
                )

        try:
            artifacts = artifact_dependency_order(package.artifacts)
        except ValueError as exc:
            return self._finish(
                plan_id,
                approval_id,
                mode,
                DeploymentStatus.FAILED,
                [],
                started,
                error=str(exc),
                package_id=package.package_id,
                plan_fingerprint=authorization.plan_fingerprint,
            )

        client = None
        if mode == DeploymentMode.MOCK:
            client = self._connector or MockFabricClient()

        steps: list[DeploymentStepResult] = []
        dependency_ids: dict[str, str] = {}
        failed = False
        for order, artifact in enumerate(artifacts, start=1):
            if failed:
                steps.append(self._skipped(order, artifact))
                continue
            if mode == DeploymentMode.DRY_RUN:
                steps.append(
                    self._step(
                        order,
                        artifact,
                        DeploymentStepStatus.SUCCEEDED,
                        message=(
                            f"DRY_RUN: validated {artifact.target_type.value} "
                            f"'{artifact.target_name}'."
                        ),
                    )
                )
                continue
            try:
                if mode == DeploymentMode.REAL:
                    outcome = real_client.deploy_artifact(artifact, dependency_ids)
                    if outcome.item_id:
                        dependency_ids[artifact.artifact_id] = outcome.item_id
                    if outcome.status == "deferred":
                        verb = "Deferred to runtime"
                    elif outcome.reused:
                        verb = "Reused"
                    else:
                        verb = "Created"
                    steps.append(
                        self._step(
                            order,
                            artifact,
                            DeploymentStepStatus.SUCCEEDED,
                            resource_id=outcome.item_id,
                            reused=outcome.reused,
                            materialization_status=outcome.materialization_status,
                            readback_status=outcome.readback_status,
                            readback_digest=outcome.readback_digest,
                            message=(
                                f"{verb} {artifact.target_type.value} "
                                f"'{artifact.target_name}'."
                            ),
                        )
                    )
                else:
                    resource_id = client.deploy_artifact(artifact)
                    steps.append(
                        self._step(
                            order,
                            artifact,
                            DeploymentStepStatus.SUCCEEDED,
                            resource_id=resource_id,
                            message=(
                                f"Deployed {artifact.target_type.value} "
                                f"'{artifact.target_name}'."
                            ),
                        )
                    )
            except (MockFabricError, FabricError) as exc:
                failed = True
                code = getattr(exc, "code", None)
                steps.append(
                    self._step(
                        order,
                        artifact,
                        DeploymentStepStatus.FAILED,
                        message=(
                            "Artifact is not deployable."
                            if code == "FABRIC_ARTIFACT_NON_DEPLOYABLE"
                            else "Artifact deployment failed."
                        ),
                        error=f"{code}: {exc}" if code else str(exc),
                        non_deployable=(code == "FABRIC_ARTIFACT_NON_DEPLOYABLE"),
                    )
                )

        return self._finish(
            plan_id,
            approval_id,
            mode,
            self._overall_status(steps),
            steps,
            started,
            package_id=package.package_id,
            plan_fingerprint=authorization.plan_fingerprint,
        )

    @staticmethod
    def _step(
        order: int,
        artifact: GeneratedArtifact,
        status: DeploymentStepStatus,
        resource_id: Optional[str] = None,
        message: str = "",
        error: Optional[str] = None,
        reused: Optional[bool] = None,
        materialization_status: Optional[str] = None,
        readback_status: Optional[str] = None,
        readback_digest: Optional[str] = None,
        non_deployable: Optional[bool] = None,
    ) -> DeploymentStepResult:
        return DeploymentStepResult(
            order=order,
            action_type="deploy_artifact",
            artifact_id=artifact.artifact_id,
            target_item_type=artifact.target_type.value,
            target_item_name=artifact.target_name,
            content_digest=artifact.content_digest,
            generated_definition=artifact.generated_definition,
            status=status,
            resource_id=resource_id,
            message=message,
            error=error,
            reused=reused,
            materialization_status=materialization_status,
            readback_status=readback_status,
            readback_digest=readback_digest,
            non_deployable=non_deployable,
        )

    @classmethod
    def _skipped(
        cls, order: int, artifact: GeneratedArtifact
    ) -> DeploymentStepResult:
        return cls._step(
            order,
            artifact,
            DeploymentStepStatus.SKIPPED,
            message="Skipped after earlier artifact failure.",
        )

    @staticmethod
    def _overall_status(
        steps: list[DeploymentStepResult],
    ) -> DeploymentStatus:
        succeeded = sum(
            step.status == DeploymentStepStatus.SUCCEEDED for step in steps
        )
        failed = sum(
            step.status == DeploymentStepStatus.FAILED for step in steps
        )
        if failed == 0:
            return DeploymentStatus.SUCCEEDED
        return (
            DeploymentStatus.PARTIAL
            if succeeded else DeploymentStatus.FAILED
        )

    def _finish(
        self,
        plan_id,
        approval_id,
        mode,
        status,
        steps,
        started,
        error=None,
        package_id=None,
        plan_fingerprint=None,
    ) -> DeploymentResult:
        succeeded = sum(
            step.status == DeploymentStepStatus.SUCCEEDED for step in steps
        )
        failed = sum(
            step.status == DeploymentStepStatus.FAILED for step in steps
        )
        skipped = sum(
            step.status == DeploymentStepStatus.SKIPPED for step in steps
        )
        resources = sum(
            step.status == DeploymentStepStatus.SUCCEEDED
            and step.resource_id is not None
            for step in steps
        )
        summary = DeploymentSummary(
            mode=mode,
            status=status,
            total_steps=len(steps),
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
            resources_created=resources,
        )
        result = DeploymentResult(
            plan_id=plan_id,
            approval_id=approval_id,
            package_id=package_id,
            plan_fingerprint=plan_fingerprint,
            mode=mode,
            status=status,
            steps=steps,
            summary=summary,
            error=error,
            started_at=started,
            completed_at=_now(),
        )
        record = save_deployment(result)
        result.deployment_id = record["id"]
        logger.info(
            "Deployment %d finished: mode=%s status=%s (%d artifacts).",
            record["id"],
            mode.value,
            status.value,
            len(steps),
        )
        return result
