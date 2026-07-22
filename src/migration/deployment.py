"""Package-aware mock Fabric deployment engine.

Deploys approved generated definitions in dependency order. DRY_RUN performs
schema/authorization checks but creates nothing; MOCK uses only the in-memory
definition-aware connector; REAL remains disabled.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from src.approvals.deployment_guard import (
    DeploymentAuthorizationError,
    validate_deployment_authorization,
)
from src.connectors.mock_fabric_client import MockFabricClient, MockFabricError
from src.migration.deployment_store import save_deployment
from src.migration.plan_store import get_plan
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
    """Raised when REAL deployment mode is requested."""


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
    """Deploy approved generated definitions to the mock connector."""

    def __init__(self, connector: Optional[MockFabricClient] = None):
        self._connector = connector

    def deploy(
        self, plan_id: int, approval_id: int, mode: DeploymentMode
    ) -> DeploymentResult:
        mode = DeploymentMode(mode)
        if mode == DeploymentMode.REAL:
            raise RealModeNotImplementedError(
                "REAL deployment mode is not implemented yet."
            )

        started = _now()
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
            except MockFabricError as exc:
                failed = True
                steps.append(
                    self._step(
                        order,
                        artifact,
                        DeploymentStepStatus.FAILED,
                        message="Artifact deployment failed.",
                        error=str(exc),
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
