"""Thin MCP tool handlers — Phase 12.

Every function here does exactly three things: (1) resolve/validate its
arguments, (2) call directly into the existing deterministic service
layer (``src.migration``, ``src.approvals``, ``src.validation``,
``src.reports``, ``src.connectors``, ``src.execution``), and (3) return
a plain dict/pydantic result. No handler here recomputes discovery,
assessment, planning, approval, deployment, or validation logic — that
would duplicate business logic the task explicitly forbids. Handlers
raise the *same* exceptions the underlying services raise; the server
layer (``src.mcp_server.server``) maps them to the sanitized envelope.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from src.approvals import approval_service
from src.approvals.approval_store import get_latest_for_plan
from src.approvals.deployment_guard import validate_deployment_authorization
from src.config import Settings, get_settings
from src.connectors.azure_adf_client import AzureDiscoveryError
from src.connectors.azure_adf_source import build_azure_adf_client_from_settings
from src.connectors.fabric_client import FabricError, build_fabric_client_from_settings
from src.execution.execution_service import (
    ExecutionAuthorizationError,
    SourceExecutionService,
    TargetExecutionService,
    source_readiness,
    target_readiness,
)
from src.execution.execution_store import DuplicateExecutionError, get_execution
from src.mcp_server.concurrency import (
    OperationInProgressError,
    advisory_lock,
    find_existing_execution_by_correlation,
    find_existing_real_deployment,
)
from src.migration.assessment import ADFCompatibilityAssessment
from src.migration.assessment_store import (
    get_assessment as _get_assessment_record,
    get_latest_assessment,
    save_assessment,
)
from src.migration.deployment import DeploymentService, FabricDeploymentDisabledError
from src.migration.deployment_store import (
    get_deployment as _get_deployment_record,
    get_latest_deployment as _get_latest_deployment_record,
    list_deployments as _list_deployments,
)
from src.migration.discovery_runner import run_discovery
from src.migration.discovery_store import get_discovery as _get_discovery_record, get_latest_discovery
from src.migration.plan_store import (
    compute_plan_package_fingerprint,
    get_latest_plan,
    get_plan as _get_plan_record_raw,
    verify_plan_package,
)
from src.migration.planner import MigrationPlanner
from src.models.schemas import DeploymentMode, DeploymentStatus
from src.reports.report_service import generate_report as _generate_report, report_path
from src.validation.runtime_execution_validation_store import (
    get_latest_runtime_execution_validation,
    get_runtime_execution_validation,
    save_runtime_execution_validation,
)
from src.validation.runtime_validation_service import RuntimeValidationError, RuntimeValidationService
from src.validation.structural_store import (
    get_latest_structural_validation,
    get_latest_structural_validation_for_plan,
    get_structural_validation,
    save_structural_validation,
)
from src.validation.structural_validator import StructuralValidationError, StructuralValidationService


class NotFoundError(Exception):
    """Raised by handlers for an unknown id. Mapped to RESOURCE_NOT_FOUND."""

    def __init__(self, what: str):
        super().__init__(what)
        self.code = "RESOURCE_NOT_FOUND"
        self.message = what


class DeploymentBlockedError(Exception):
    """Raised when DeploymentService.deploy() returns DeploymentStatus.BLOCKED
    (authorization refused the deployment — e.g. approval not APPROVED,
    fingerprint/version mismatch, package missing/modified). The deployment
    IS still persisted with the underlying error message reused verbatim,
    reusing DeploymentAuthorizationError's code rather than reimplementing
    it, since ``result.error`` is formatted as "CODE: message"."""

    def __init__(self, error_text: str, deployment_id: Optional[int]):
        super().__init__(error_text)
        code, _, message = (error_text or "").partition(": ")
        self.code = code or "DEPLOYMENT_BLOCKED"
        self.message = message or error_text or "Deployment was blocked."
        self.deployment_id = deployment_id


# ── Read-only ────────────────────────────────────────────────────


def health_status() -> dict:
    settings = get_settings()
    return {"status": "ok", "service": settings.app_name, "environment": settings.app_env}


def capability_status() -> dict:
    settings = get_settings()
    return {
        "config": settings.safe_dict(),
        "azure_discovery_ready": settings.azure_discovery_ready(),
        "fabric_deployment_ready": settings.fabric_deployment_ready(),
        "runtime_execution_ready": settings.runtime_execution_ready(),
        "migration_dry_run": settings.migration_dry_run,
        "migration_require_approval": settings.migration_require_approval,
        "migration_allow_delete": settings.migration_allow_delete,
    }


def verify_azure_environment() -> dict:
    settings = get_settings()
    client = build_azure_adf_client_from_settings(settings)
    environment = client.verify_environment()
    data_factory = client.verify_data_factory()
    return {"environment": environment, "data_factory": data_factory}


def verify_fabric_environment() -> dict:
    settings = get_settings()
    client = build_fabric_client_from_settings(settings)
    return client.verify_environment()


def get_discovery(discovery_id: Optional[int] = None) -> dict:
    record = _get_discovery_record(discovery_id) if discovery_id is not None else get_latest_discovery()
    if record is None:
        raise NotFoundError(
            f"Discovery {discovery_id} not found." if discovery_id is not None
            else "No discovery scan has been run yet."
        )
    return {
        "discovery_id": record["id"],
        "artifact_count": record["artifact_count"],
        "component_count": record["component_count"],
        "created_at": record["created_at"],
        "summary": record["result"].summary.model_dump(mode="json"),
    }


def get_dependencies(discovery_id: Optional[int] = None) -> dict:
    record = _get_discovery_record(discovery_id) if discovery_id is not None else get_latest_discovery()
    if record is None:
        raise NotFoundError("No discovery scan has been run yet.")
    result = record["result"]
    return {
        "discovery_id": record["id"],
        "dependency_count": len(result.dependencies),
        "dependencies": [d.model_dump(mode="json") for d in result.dependencies],
        "missing_count": len(result.missing_dependencies),
        "missing_dependencies": [m.model_dump(mode="json") for m in result.missing_dependencies],
    }


def get_assessment(assessment_id: Optional[int] = None) -> dict:
    record = _get_assessment_record(assessment_id) if assessment_id is not None else get_latest_assessment()
    if record is None:
        raise NotFoundError(
            f"Assessment {assessment_id} not found." if assessment_id is not None
            else "No assessment has been run yet."
        )
    return {
        "assessment_id": record["id"],
        "created_at": record["created_at"],
        "overall_status": record["overall_status"],
        "summary": record["result"].summary.model_dump(mode="json"),
    }


def _get_plan_record(plan_id: Optional[int]) -> Optional[dict]:
    """Return the raw persisted plan record ({id, plan, ...}) by id, or the
    latest one if plan_id is None."""
    return _get_plan_record_raw(plan_id) if plan_id is not None else get_latest_plan()


def _require_plan_record(plan_id: Optional[int]) -> dict:
    record = _get_plan_record(plan_id)
    if record is None:
        raise NotFoundError(
            f"Plan {plan_id} not found." if plan_id is not None else "No plan has been generated yet."
        )
    return record


def get_plan(plan_id: Optional[int] = None) -> dict:
    record = _require_plan_record(plan_id)
    plan = record["plan"]
    return {
        "plan_id": record["id"],
        "assessment_id": record["assessment_id"],
        "version": record["version"],
        "executable": record["executable"],
        "overall_risk": record["overall_risk"],
        "created_at": record["created_at"],
        "summary": plan.summary.model_dump(mode="json"),
        "mapping_count": len(plan.mappings),
        "action_count": len(plan.actions),
        "manual_action_count": len(plan.manual_actions),
        "has_generated_package": plan.generated_package is not None,
    }


def get_package_summary(plan_id: int) -> dict:
    record = _require_plan_record(plan_id)
    package = record["plan"].generated_package
    if package is None:
        raise NotFoundError(f"Plan {plan_id} has no generated package.")
    return {
        "plan_id": plan_id,
        "package_id": package.package_id,
        "package_digest": package.manifest.package_digest,
        "artifact_count": len(package.artifacts),
        "artifacts": [
            {
                "artifact_id": a.artifact_id,
                "target_type": a.target_type.value,
                "target_name": a.target_name,
                "content_digest": a.content_digest,
                "warning_count": len(a.warnings),
                "unsupported_property_count": len(a.unsupported_properties),
                "manual_action_count": len(a.manual_actions),
                "dependencies": a.dependencies,
            }
            for a in package.artifacts
        ],
    }


def get_manifest_summary(plan_id: int) -> dict:
    record = _require_plan_record(plan_id)
    package = record["plan"].generated_package
    if package is None:
        raise NotFoundError(f"Plan {plan_id} has no generated package.")
    manifest = package.manifest
    return {
        "plan_id": plan_id,
        "package_id": manifest.package_id,
        "schema_version": manifest.schema_version,
        "package_digest": manifest.package_digest,
        "entry_count": len(manifest.entries),
        "entries": [
            {
                "artifact_id": e.artifact_id,
                "target_type": e.target_type.value,
                "target_name": e.target_name,
                "relative_path": Path(e.relative_path).as_posix(),
                "content_digest": e.content_digest,
                "dependencies": e.dependencies,
            }
            for e in manifest.entries
        ],
    }


def get_approval_status(plan_id: int) -> dict:
    _require_plan_record(plan_id)
    latest = get_latest_for_plan(plan_id)
    if latest is None:
        return {"plan_id": plan_id, "status": "NONE", "approval": None, "can_deploy": False}
    return {
        "plan_id": plan_id,
        "status": latest.status.value,
        "approval": latest.model_dump(mode="json"),
        "can_deploy": approval_service.can_deploy(plan_id, latest.approval_id),
    }


def _get_deployment_raw(deployment_id: Optional[int]) -> Optional[dict]:
    return (
        _get_deployment_record(deployment_id) if deployment_id is not None
        else _get_latest_deployment_record()
    )


def get_deployment(deployment_id: Optional[int] = None) -> dict:
    record = _get_deployment_raw(deployment_id)
    if record is None:
        raise NotFoundError(
            f"Deployment {deployment_id} not found." if deployment_id is not None
            else "No deployment has been run yet."
        )
    return _serialize_deployment(record)


def _serialize_deployment(record: dict) -> dict:
    result = record["result"]
    return {
        "deployment_id": record["id"],
        "plan_id": record["plan_id"],
        "approval_id": record["approval_id"],
        "mode": record["mode"],
        "status": record["status"],
        "created_at": record["created_at"],
        "completed_at": record["completed_at"],
        "summary": result.summary.model_dump(mode="json") if result.summary else None,
        "step_count": len(result.steps),
        "error": result.error,
    }


def get_execution_tool(execution_id: int) -> dict:
    result = get_execution(execution_id)
    if result is None:
        raise NotFoundError(f"Execution {execution_id} not found.")
    return result.model_dump(mode="json")


def get_structural_validation_tool(validation_id: Optional[int] = None) -> dict:
    result = get_structural_validation(validation_id) if validation_id is not None else get_latest_structural_validation()
    if result is None:
        raise NotFoundError(
            f"Structural validation {validation_id} not found." if validation_id is not None
            else "No structural validation has been run yet."
        )
    return result.model_dump(mode="json")


def get_runtime_validation_tool(
    validation_id: Optional[int] = None, plan_id: Optional[int] = None
) -> dict:
    if validation_id is not None:
        result = get_runtime_execution_validation(validation_id)
    else:
        result = get_latest_runtime_execution_validation(plan_id=plan_id)
    if result is None:
        raise NotFoundError("No runtime-equivalence validation found for the given id/plan.")
    return result.model_dump(mode="json")


def get_report_tool(validation_id: int, full: bool = False) -> dict:
    validation = get_structural_validation(validation_id)
    if validation is None:
        raise NotFoundError(f"Structural validation {validation_id} not found.")
    json_path = report_path(validation_id, "json")
    html_path = report_path(validation_id, "html")
    exists = json_path.exists()
    summary = {
        "validation_id": validation_id,
        "report_exists": exists,
        "status": validation.status.value,
        "summary": validation.summary.model_dump(mode="json"),
    }
    if not exists:
        return summary
    if full:
        summary["report_json"] = json_path.read_text(encoding="utf-8")
    return summary


def get_final_migration_status(plan_id: int) -> dict:
    plan_record = _require_plan_record(plan_id)
    approval = get_latest_for_plan(plan_id)
    deployment_meta = None
    for meta in _list_deployments():
        if meta["plan_id"] == plan_id:
            deployment_meta = meta
            break
    structural = get_latest_structural_validation_for_plan(plan_id)
    runtime = get_latest_runtime_execution_validation(plan_id=plan_id)
    report_exists = (
        structural is not None and report_path(structural.validation_id, "json").exists()
    )
    return {
        "plan_id": plan_id,
        "plan_version": plan_record["version"],
        "plan_executable": plan_record["executable"],
        "approval": (
            {"approval_id": approval.approval_id, "status": approval.status.value}
            if approval else None
        ),
        "deployment": (
            {"deployment_id": deployment_meta["id"], "mode": deployment_meta["mode"],
             "status": deployment_meta["status"]}
            if deployment_meta else None
        ),
        "structural_validation": (
            {"validation_id": structural.validation_id, "status": structural.status.value}
            if structural else None
        ),
        "runtime_validation": (
            {"validation_id": runtime.validation_id, "status": runtime.status.value}
            if runtime else None
        ),
        "report_available": report_exists,
    }


# ── State change (real, local, non-cloud) ───────────────────────


def scan_adf(mode: str = "fixture") -> dict:
    record = run_discovery(mode)
    return {
        "status": "completed",
        "mode": mode,
        "discovery_id": record["id"],
        "summary": record["result"].summary.model_dump(mode="json"),
    }


def run_assessment_tool() -> dict:
    discovery_record = get_latest_discovery()
    if discovery_record is None:
        raise NotFoundError("No discovery scan found. Run scan_adf first.")
    inventory = discovery_record["result"].inventory
    engine = ADFCompatibilityAssessment(inventory)
    result = engine.assess_discovery(discovery_record["result"])
    result.discovery_id = discovery_record["id"]
    assessment_id = save_assessment(result)
    return {
        "status": "completed",
        "assessment_id": assessment_id,
        "overall_status": result.overall_status.value,
        "summary": result.summary.model_dump(mode="json"),
    }


def generate_plan_tool() -> dict:
    discovery_record = get_latest_discovery()
    if discovery_record is None:
        raise NotFoundError("No discovery scan found. Run scan_adf first.")
    assessment_record = get_latest_assessment()
    if assessment_record is None:
        raise NotFoundError("No assessment found. Run run_assessment first.")
    inventory = discovery_record["result"].inventory
    plan = MigrationPlanner(inventory).generate_plan(
        discovery_record["result"], assessment_record["result"], discovery_record["id"]
    )
    from src.migration.plan_store import save_plan

    record = save_plan(plan, assessment_id=assessment_record["id"])
    try:
        approval_service.invalidate_stale_approvals(record["id"])
    except Exception:  # pragma: no cover - approvals are non-critical here
        pass
    return {
        "status": "completed",
        "plan_id": record["id"],
        "version": record["version"],
        "executable": record["executable"],
        "overall_risk": record["overall_risk"],
        "package_id": plan.generated_package.package_id if plan.generated_package else None,
        "generated_artifact_count": (
            len(plan.generated_package.artifacts) if plan.generated_package else 0
        ),
        "summary": plan.summary.model_dump(mode="json"),
    }


def request_approval_tool(plan_id: int, requested_by: str, comment: str = "") -> dict:
    """Create a PENDING approval request only. There is deliberately no
    corresponding "approve"/"reject" MCP tool: approval decisions can only
    be made through the existing human approval workflow
    (``src.approvals.approval_service`` via the web UI), never by an MCP
    caller supplying a free-text "approved" shortcut."""
    result = approval_service.request_approval(plan_id, requested_by, comment)
    return result.model_dump(mode="json")


def run_structural_validation_tool(deployment_id: int) -> dict:
    result = StructuralValidationService().validate(deployment_id)
    saved = save_structural_validation(result)
    return saved.model_dump(mode="json")


def run_runtime_validation_tool(source_execution_id: int, target_execution_id: int) -> dict:
    result = RuntimeValidationService().validate(source_execution_id, target_execution_id)
    saved = save_runtime_execution_validation(result)
    return saved.model_dump(mode="json")


def generate_report_tool(validation_id: int, force: bool = False) -> dict:
    json_path = report_path(validation_id, "json")
    if json_path.exists() and not force:
        validation = get_structural_validation(validation_id)
        return {
            "status": "already_exists",
            "validation_id": validation_id,
            "report_exists": True,
            "reused": True,
            "status_value": validation.status.value if validation else None,
        }
    report = _generate_report(validation_id)
    return {
        "status": "completed",
        "validation_id": validation_id,
        "report_id": report.report_id,
        "generated_at": report.generated_at,
        "report_exists": True,
        "reused": False,
    }


# ── Guarded cloud write ──────────────────────────────────────────


def deploy_fabric_package_tool(
    plan_id: int, approval_id: int, mode: str, correlation_id: str
) -> dict:
    deployment_mode = DeploymentMode(mode)

    if deployment_mode == DeploymentMode.REAL:
        existing = find_existing_real_deployment(plan_id, approval_id)
        if existing is not None:
            serialized = _serialize_deployment(existing)
            serialized["reused"] = True
            return serialized

    lock_key = f"{plan_id}:{approval_id}:{deployment_mode.value}"
    with advisory_lock("deploy_fabric_package", lock_key, correlation_id):
        result = DeploymentService().deploy(plan_id, approval_id, deployment_mode)

    if result.status == DeploymentStatus.BLOCKED:
        # Authorization refused the deployment (reuses
        # validate_deployment_authorization's own error code/message —
        # never reimplemented here). The BLOCKED record is still saved.
        raise DeploymentBlockedError(result.error or "", result.deployment_id)

    serialized = {
        "deployment_id": result.deployment_id,
        "plan_id": result.plan_id,
        "approval_id": result.approval_id,
        "mode": result.mode.value,
        "status": result.status.value,
        "summary": result.summary.model_dump(mode="json") if result.summary else None,
        "step_count": len(result.steps),
        "error": result.error,
        "reused": False,
    }
    return serialized


# ── Guarded execution ─────────────────────────────────────────────


def run_source_pipeline_tool(
    plan_id: Optional[int],
    deployment_id: Optional[int],
    discovery_snapshot_id: Optional[int],
    correlation_id: str,
) -> dict:
    existing = find_existing_execution_by_correlation(correlation_id)
    if existing is not None:
        existing["reused"] = True
        return existing

    lock_key = f"source:{correlation_id}"
    with advisory_lock("run_source_pipeline", lock_key, correlation_id):
        result = SourceExecutionService().start(
            plan_id=plan_id,
            deployment_id=deployment_id,
            discovery_snapshot_id=discovery_snapshot_id,
            correlation_id=correlation_id,
        )
    payload = result.model_dump(mode="json")
    payload["reused"] = False
    return payload


def run_fabric_pipeline_tool(
    plan_id: int, deployment_id: int, correlation_id: str
) -> dict:
    existing = find_existing_execution_by_correlation(correlation_id)
    if existing is not None:
        existing["reused"] = True
        return existing

    lock_key = f"target:{correlation_id}"
    with advisory_lock("run_fabric_pipeline", lock_key, correlation_id):
        result = TargetExecutionService().start(
            plan_id=plan_id, deployment_id=deployment_id, correlation_id=correlation_id
        )
    payload = result.model_dump(mode="json")
    payload["reused"] = False
    return payload
