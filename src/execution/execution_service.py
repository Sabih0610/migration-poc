"""Controlled execution orchestration — Phase 11.

Orchestrates the boundary-checked source (ADF) and target (Fabric)
executors, persists safe run metadata via ``execution_store``, and — for
target execution only — enforces every pre-execution authorization check
required before a real Fabric pipeline job may be started. Runtime
metrics are collected through an injectable ``MetricsProvider``.

Both direct Python usage and the FastAPI routes in
``src.api.execution_routes`` go through this one service, so the same
guarantees apply regardless of caller.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from src.approvals.approval_store import get_approval
from src.artifacts import ArtifactPackageError
from src.config import Settings, get_settings
from src.connectors.azure_adf_executor import (
    AzureADFExecutor,
    AzureExecutionError,
    build_azure_adf_executor_from_settings,
)
from src.connectors.fabric_client import FabricError
from src.connectors.fabric_pipeline_executor import (
    FabricPipelineExecutor,
    build_fabric_pipeline_executor_from_settings,
)
from src.execution.execution_store import (
    DuplicateExecutionError,
    complete_execution,
    get_running_execution,
    start_execution,
)
from src.execution.metrics_provider import MetricsProvider, MockMetricsProvider
from src.migration.deployment_store import get_deployment
from src.migration.plan_store import (
    compute_plan_package_fingerprint,
    get_plan,
    verify_plan_package,
)
from src.models.schemas import (
    DeployableTargetType,
    DeploymentMode,
    DeploymentStatus,
    ExecutionSide,
    ExecutionStatus,
    PipelineExecutionResult,
    ValidationStatus,
)
from src.validation.structural_store import get_latest_structural_validation_for_plan

logger = logging.getLogger(__name__)

# Structural statuses that are considered a "PASS" for execution gating.
_STRUCTURAL_PASS_STATUSES = {
    ValidationStatus.PASSED,
    ValidationStatus.PASSED_WITH_WARNINGS,
}


class ExecutionAuthorizationError(Exception):
    """Raised when a controlled execution is not authorized. Never starts
    an execution when raised."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code


def _new_correlation_id() -> str:
    return uuid.uuid4().hex


def source_readiness(settings: Optional[Settings] = None) -> dict:
    """Report controlled source (ADF) execution readiness (no network)."""
    settings = settings or get_settings()
    missing = [
        name
        for name in (
            "azure_tenant_id",
            "azure_client_id",
            "azure_client_secret",
            "azure_subscription_id",
            "azure_resource_group",
            "azure_data_factory_name",
            "adf_source_pipeline_name",
        )
        if not getattr(settings, name, "")
    ]
    return {
        "enabled": settings.runtime_execution_enabled,
        "configured": not missing,
        "ready": settings.runtime_execution_enabled and not missing,
        "missing_settings": missing,
        "pipeline_name": settings.adf_source_pipeline_name or None,
        "data_factory_name": settings.azure_data_factory_name or None,
    }


def target_readiness(settings: Optional[Settings] = None) -> dict:
    """Report controlled target (Fabric) execution readiness (no network)."""
    settings = settings or get_settings()
    missing = [
        name
        for name in (
            "fabric_tenant_id",
            "fabric_client_id",
            "fabric_client_secret",
            "fabric_workspace_id",
            "fabric_target_pipeline_item_id",
        )
        if not getattr(settings, name, "")
    ]
    return {
        "enabled": settings.runtime_execution_enabled,
        "configured": not missing,
        "ready": settings.runtime_execution_enabled and not missing,
        "missing_settings": missing,
        "item_id": settings.fabric_target_pipeline_item_id or None,
        "workspace_id": settings.fabric_workspace_id or None,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SourceExecutionService:
    """Runs the controlled source ADF pipeline and persists the result."""

    def __init__(self, executor: Optional[AzureADFExecutor] = None):
        self._executor = executor

    def _resolve_executor(self, settings: Settings) -> AzureADFExecutor:
        if self._executor is not None:
            return self._executor
        return build_azure_adf_executor_from_settings(settings)

    def start(
        self,
        *,
        plan_id: Optional[int] = None,
        deployment_id: Optional[int] = None,
        discovery_snapshot_id: Optional[int] = None,
        correlation_id: Optional[str] = None,
        metrics_provider: Optional[MetricsProvider] = None,
    ) -> PipelineExecutionResult:
        settings = get_settings()
        if not settings.runtime_execution_ready():
            raise AzureExecutionError(
                "Runtime execution requires RUNTIME_EXECUTION_ENABLED=true "
                "and full Azure/ADF source configuration.",
                "ADF_EXECUTION_DISABLED",
            )
        executor = self._resolve_executor(settings)
        correlation_id = correlation_id or _new_correlation_id()

        existing = get_running_execution(ExecutionSide.SOURCE, executor.pipeline_name)
        if existing is not None:
            raise DuplicateExecutionError(
                f"Source pipeline '{executor.pipeline_name}' already has a "
                f"running execution (execution_id={existing.execution_id}).",
                existing.execution_id,
            )

        record = start_execution(
            correlation_id=correlation_id,
            side=ExecutionSide.SOURCE,
            pipeline_identity=executor.pipeline_name,
            plan_id=plan_id,
            deployment_id=deployment_id,
            discovery_snapshot_id=discovery_snapshot_id,
        )
        try:
            run = executor.run_to_terminal(executor.pipeline_name)
        except AzureExecutionError as exc:
            return complete_execution(
                record.execution_id,
                status=ExecutionStatus.FAILED,
                safe_error_category=exc.code,
            )

        status = _map_source_status(run.status)
        provider = metrics_provider or MockMetricsProvider()
        metrics = None
        if status == ExecutionStatus.SUCCEEDED:
            metrics = provider.collect(ExecutionSide.SOURCE, executor.pipeline_name, run.run_id)
        return complete_execution(
            record.execution_id,
            status=status,
            run_id=run.run_id,
            safe_error_category=run.safe_error_category,
            duration_seconds=run.duration_seconds,
            metrics=metrics,
        )


class TargetExecutionService:
    """Runs the controlled target Fabric pipeline after full authorization."""

    def __init__(self, executor: Optional[FabricPipelineExecutor] = None):
        self._executor = executor

    def _resolve_executor(self, settings: Settings) -> FabricPipelineExecutor:
        if self._executor is not None:
            return self._executor
        return build_fabric_pipeline_executor_from_settings(settings)

    def _authorize(self, plan_id: int, deployment_id: int, settings: Settings) -> None:
        """Raise ExecutionAuthorizationError on the first failed check."""
        if not settings.runtime_execution_ready():
            raise ExecutionAuthorizationError(
                "Runtime execution requires RUNTIME_EXECUTION_ENABLED=true "
                "and full Azure/Fabric configuration.",
                "RUNTIME_EXECUTION_DISABLED",
            )

        deployment_record = get_deployment(deployment_id)
        if deployment_record is None:
            raise ExecutionAuthorizationError(
                f"Deployment {deployment_id} does not exist.", "DEPLOYMENT_NOT_FOUND"
            )
        deployment = deployment_record["result"]
        if deployment.plan_id != plan_id:
            raise ExecutionAuthorizationError(
                f"Deployment {deployment_id} is bound to plan "
                f"{deployment.plan_id}, not {plan_id}.",
                "PLAN_ID_MISMATCH",
            )
        if deployment.mode != DeploymentMode.REAL:
            raise ExecutionAuthorizationError(
                f"Deployment {deployment_id} is not a REAL deployment "
                f"(mode={deployment.mode.value}).",
                "DEPLOYMENT_NOT_REAL",
            )
        if deployment.status != DeploymentStatus.SUCCEEDED:
            raise ExecutionAuthorizationError(
                f"Deployment {deployment_id} did not complete successfully "
                f"(status={deployment.status.value}).",
                "DEPLOYMENT_NOT_SUCCESSFUL",
            )

        plan_record = get_plan(plan_id)
        if plan_record is None:
            raise ExecutionAuthorizationError(
                f"Plan {plan_id} does not exist.", "PLAN_NOT_FOUND"
            )
        plan = plan_record["plan"]

        approval = get_approval(deployment.approval_id) if deployment.approval_id else None
        if approval is None:
            raise ExecutionAuthorizationError(
                "Deployment has no associated approval.", "APPROVAL_NOT_FOUND"
            )
        if approval.plan_version != plan_record["version"]:
            raise ExecutionAuthorizationError(
                "Approved plan version does not match the current plan "
                "version.",
                "PLAN_VERSION_MISMATCH",
            )

        current_fingerprint = compute_plan_package_fingerprint(plan)
        if (
            deployment.plan_fingerprint != current_fingerprint
            or approval.plan_fingerprint != current_fingerprint
        ):
            raise ExecutionAuthorizationError(
                "Package fingerprint has changed since approval/deployment.",
                "PACKAGE_FINGERPRINT_MISMATCH",
            )

        try:
            verify_plan_package(plan)
        except ArtifactPackageError as exc:
            raise ExecutionAuthorizationError(
                f"Approved package verification failed: {exc}",
                "PACKAGE_VERIFICATION_FAILED",
            ) from exc

        target_item_id = settings.fabric_target_pipeline_item_id
        matching_step = next(
            (
                step
                for step in deployment.steps
                if step.target_item_type == DeployableTargetType.DATA_PIPELINE.value
                and step.resource_id == target_item_id
            ),
            None,
        )
        if matching_step is None:
            raise ExecutionAuthorizationError(
                f"Deployment {deployment_id} has no deployed Data Pipeline "
                f"matching the configured target item id.",
                "PIPELINE_ITEM_NOT_DEPLOYED",
            )
        readback_status = getattr(matching_step, "readback_status", None)
        deployed_digest = getattr(matching_step, "readback_digest", None) or matching_step.content_digest
        if not deployed_digest:
            raise ExecutionAuthorizationError(
                "The deployed pipeline item has no recorded definition "
                "digest to verify against.",
                "DEPLOYED_DIGEST_MISSING",
            )
        if readback_status is not None and readback_status not in ("MATCH", "UNSUPPORTED"):
            raise ExecutionAuthorizationError(
                "The deployed pipeline item's read-back definition digest "
                "does not match what was approved.",
                "DEPLOYED_DIGEST_MISMATCH",
            )

        structural = get_latest_structural_validation_for_plan(plan_id)
        if structural is None or structural.status not in _STRUCTURAL_PASS_STATUSES:
            raise ExecutionAuthorizationError(
                "Structural validation for this deployment has not passed.",
                "STRUCTURAL_VALIDATION_NOT_PASSED",
            )

    def start(
        self,
        *,
        plan_id: int,
        deployment_id: int,
        correlation_id: Optional[str] = None,
        metrics_provider: Optional[MetricsProvider] = None,
    ) -> PipelineExecutionResult:
        settings = get_settings()
        self._authorize(plan_id, deployment_id, settings)
        executor = self._resolve_executor(settings)
        correlation_id = correlation_id or _new_correlation_id()

        existing = get_running_execution(ExecutionSide.TARGET, executor.item_id)
        if existing is not None:
            raise DuplicateExecutionError(
                f"Target pipeline '{executor.item_id}' already has a "
                f"running execution (execution_id={existing.execution_id}).",
                existing.execution_id,
            )

        record = start_execution(
            correlation_id=correlation_id,
            side=ExecutionSide.TARGET,
            pipeline_identity=executor.item_id,
            plan_id=plan_id,
            deployment_id=deployment_id,
        )
        try:
            run = executor.run_to_terminal(executor.item_id)
        except FabricError as exc:
            return complete_execution(
                record.execution_id,
                status=ExecutionStatus.FAILED,
                safe_error_category=exc.code,
            )

        status = _map_target_status(run.status)
        provider = metrics_provider or MockMetricsProvider()
        metrics = None
        if status == ExecutionStatus.SUCCEEDED:
            metrics = provider.collect(ExecutionSide.TARGET, executor.item_id, run.job_instance_id)
        return complete_execution(
            record.execution_id,
            status=status,
            run_id=run.job_instance_id,
            safe_error_category=run.safe_error_category,
            duration_seconds=run.duration_seconds,
            metrics=metrics,
        )


def _map_source_status(adf_status: str) -> ExecutionStatus:
    if adf_status == "Succeeded":
        return ExecutionStatus.SUCCEEDED
    if adf_status == "TimedOut":
        return ExecutionStatus.TIMED_OUT
    if adf_status == "Cancelled":
        return ExecutionStatus.CANCELLED
    return ExecutionStatus.FAILED


def _map_target_status(fabric_status: str) -> ExecutionStatus:
    if fabric_status == "Completed":
        return ExecutionStatus.SUCCEEDED
    if fabric_status == "TimedOut":
        return ExecutionStatus.TIMED_OUT
    if fabric_status == "Cancelled":
        return ExecutionStatus.CANCELLED
    return ExecutionStatus.FAILED
