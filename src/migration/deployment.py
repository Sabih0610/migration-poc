"""Mock Fabric Deployment Engine — Phase 7.

Executes an approved migration plan in DRY_RUN or MOCK mode. Makes no
real Fabric calls. Deployment is gated by the Phase 6 deployment guard,
executes actions in plan order, records every step, and stops safely on
failure. The VALIDATE step is deferred to Phase 8.
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
    MigrationActionType,
)

logger = logging.getLogger(__name__)

# Plan action type -> mock client method name.
_METHOD_FOR_ACTION = {
    MigrationActionType.VERIFY_WORKSPACE: "verify_workspace",
    MigrationActionType.CREATE_CONNECTION: "create_connection",
    MigrationActionType.CREATE_LAKEHOUSE: "create_lakehouse",
    MigrationActionType.CREATE_TABLE: "create_table",
    MigrationActionType.CREATE_DATAFLOW: "create_dataflow",
    MigrationActionType.CREATE_PIPELINE: "create_pipeline",
    MigrationActionType.CONFIGURE_SCHEDULE: "configure_schedule",
    MigrationActionType.RUN_TARGET: "run_target",
}

# Actions that create a tracked resource (for resources_created count).
_CREATION_ACTIONS = {
    MigrationActionType.CREATE_CONNECTION,
    MigrationActionType.CREATE_LAKEHOUSE,
    MigrationActionType.CREATE_TABLE,
    MigrationActionType.CREATE_DATAFLOW,
    MigrationActionType.CREATE_PIPELINE,
    MigrationActionType.CONFIGURE_SCHEDULE,
}


class RealModeNotImplementedError(Exception):
    """Raised when REAL deployment mode is requested (not yet available)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeploymentService:
    """Executes approved plans against a mock (or dry-run) Fabric target."""

    def __init__(self, connector: Optional[MockFabricClient] = None):
        # Optional injected connector lets callers share state across runs
        # (used to prove idempotency). Defaults to a fresh client per deploy.
        self._connector = connector

    def deploy(
        self, plan_id: int, approval_id: int, mode: DeploymentMode
    ) -> DeploymentResult:
        """Deploy an approved plan. Returns a persisted DeploymentResult."""
        mode = DeploymentMode(mode)
        if mode == DeploymentMode.REAL:
            raise RealModeNotImplementedError(
                "REAL deployment mode is not implemented yet."
            )

        started = _now()

        # ── Authorization gate ───────────────────────────────
        try:
            validate_deployment_authorization(plan_id, approval_id)
        except DeploymentAuthorizationError as exc:
            return self._finish(
                plan_id, approval_id, mode, DeploymentStatus.BLOCKED,
                steps=[], started=started, error=f"{exc.code}: {exc.message}",
            )

        plan = get_plan(plan_id)["plan"]
        client = None
        if mode == DeploymentMode.MOCK:
            client = self._connector or MockFabricClient()

        steps: list[DeploymentStepResult] = []
        failed = False

        for action in plan.actions:
            action_type = MigrationActionType(action.action_type)

            # Once a step fails, stop safely — record the rest as SKIPPED.
            if failed:
                steps.append(self._skipped(action, "Skipped after earlier failure."))
                continue

            # VALIDATE is deferred to Phase 8.
            if action_type == MigrationActionType.VALIDATE:
                steps.append(
                    self._skipped(action, "Validation deferred to Phase 8.")
                )
                continue

            if mode == DeploymentMode.DRY_RUN:
                steps.append(
                    DeploymentStepResult(
                        order=action.order,
                        action_type=action.action_type,
                        target_item_type=action.target_item_type,
                        target_item_name=action.target_item_name,
                        status=DeploymentStepStatus.SUCCEEDED,
                        resource_id=None,
                        message=f"DRY_RUN: would {action.action_type} "
                        f"'{action.target_item_name}'.",
                    )
                )
                continue

            # MOCK mode — call the connector.
            method_name = _METHOD_FOR_ACTION.get(action_type)
            if method_name is None:
                steps.append(self._skipped(action, "No connector for this action."))
                continue
            try:
                resource_id = getattr(client, method_name)(action.target_item_name)
                steps.append(
                    DeploymentStepResult(
                        order=action.order,
                        action_type=action.action_type,
                        target_item_type=action.target_item_type,
                        target_item_name=action.target_item_name,
                        status=DeploymentStepStatus.SUCCEEDED,
                        resource_id=resource_id,
                        message=f"{action.action_type} '{action.target_item_name}'.",
                    )
                )
            except MockFabricError as exc:
                failed = True
                steps.append(
                    DeploymentStepResult(
                        order=action.order,
                        action_type=action.action_type,
                        target_item_type=action.target_item_type,
                        target_item_name=action.target_item_name,
                        status=DeploymentStepStatus.FAILED,
                        message="Step failed.",
                        error=str(exc),
                    )
                )

        status = self._overall_status(steps)
        return self._finish(
            plan_id, approval_id, mode, status, steps=steps, started=started,
        )

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _skipped(action, message: str) -> DeploymentStepResult:
        return DeploymentStepResult(
            order=action.order,
            action_type=action.action_type,
            target_item_type=action.target_item_type,
            target_item_name=action.target_item_name,
            status=DeploymentStepStatus.SKIPPED,
            message=message,
        )

    @staticmethod
    def _overall_status(steps: list[DeploymentStepResult]) -> DeploymentStatus:
        succeeded = sum(
            1 for s in steps if s.status == DeploymentStepStatus.SUCCEEDED
        )
        failed = sum(1 for s in steps if s.status == DeploymentStepStatus.FAILED)
        if failed == 0:
            return DeploymentStatus.SUCCEEDED
        if succeeded > 0:
            return DeploymentStatus.PARTIAL
        return DeploymentStatus.FAILED

    def _finish(
        self, plan_id, approval_id, mode, status, steps, started, error=None
    ) -> DeploymentResult:
        succeeded = sum(
            1 for s in steps if s.status == DeploymentStepStatus.SUCCEEDED
        )
        failed = sum(1 for s in steps if s.status == DeploymentStepStatus.FAILED)
        skipped = sum(1 for s in steps if s.status == DeploymentStepStatus.SKIPPED)
        resources = sum(
            1
            for s in steps
            if s.status == DeploymentStepStatus.SUCCEEDED
            and s.resource_id is not None
            and MigrationActionType(s.action_type) in _CREATION_ACTIONS
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
            "Deployment %d finished: mode=%s status=%s (%d steps).",
            record["id"], mode.value, status.value, len(steps),
        )
        return result
