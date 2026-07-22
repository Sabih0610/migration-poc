"""Phase 12 — local Python STDIO MCP server.

Pure controlled-interface layer: every tool function below is a thin
wrapper that (1) enforces authorization gates BEFORE calling into the
service layer, (2) delegates to ``src.mcp_server.handlers`` (which in
turn calls the existing deterministic services), (3) builds the shared
response envelope, and (4) writes one audit row. No business logic
(discovery/assessment/planning/approval/deployment/validation/reporting)
is implemented here or in ``handlers.py`` — it all lives in the existing
``src.migration`` / ``src.approvals`` / ``src.validation`` / ``src.reports``
/ ``src.execution`` / ``src.connectors`` modules, unmodified.

Transport: local STDIO only (``mcp.server.fastmcp.FastMCP.run("stdio")``).
There is no HTTP listener here; the FastAPI app in ``src.api.app`` is a
completely separate, unaffected process.

Launch:
    python -m src.mcp_server.server
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Optional

from mcp.server.fastmcp import FastMCP

from src.config import get_settings
from src.mcp_server import handlers
from src.mcp_server.audit_store import record_audit
from src.mcp_server.concurrency import OperationInProgressError
from src.mcp_server.envelope import build_envelope, map_exception
from src.mcp_server.permissions import (
    GUARDED_CLOUD_WRITE,
    GUARDED_EXECUTION,
    READ_ONLY,
    STATE_CHANGE,
    next_allowed_actions,
    permission_category,
)

mcp = FastMCP(
    name="migration-poc-mcp",
    instructions=(
        "Controlled interface over the ADF -> Fabric migration PoC's "
        "existing deterministic services. Read-only tools query "
        "persisted state; state-change tools run local (non-cloud) "
        "workflow steps; guarded tools require explicit configuration "
        "and a persisted, human-issued approval before any Fabric write "
        "or pipeline execution is attempted."
    ),
)


class ToolAuthorizationError(Exception):
    """Raised by a pre-check before any service call is made."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _new_correlation_id() -> str:
    return uuid.uuid4().hex


def _authorize(tool_name: str, category: str, kwargs: dict) -> None:
    """Enforce gates BEFORE the handler (and therefore the service layer)
    is ever called. Raises ToolAuthorizationError on failure."""
    settings = get_settings()

    if category == GUARDED_CLOUD_WRITE:
        mode = str(kwargs.get("mode", "")).upper()
        if mode == "REAL" and not settings.fabric_deployment_ready():
            raise ToolAuthorizationError(
                "FABRIC_DEPLOYMENT_DISABLED",
                "REAL deployment requires FABRIC_DEPLOYMENT_ENABLED=true and "
                "full Fabric configuration.",
            )

    if category == GUARDED_EXECUTION:
        if not settings.runtime_execution_ready():
            raise ToolAuthorizationError(
                "RUNTIME_EXECUTION_DISABLED",
                "Runtime execution requires RUNTIME_EXECUTION_ENABLED=true "
                "and full Azure/Fabric configuration.",
            )


def _invoke(
    tool_name: str,
    fn: Callable[..., Any],
    kwargs: dict,
    *,
    referenced_ids: Optional[dict] = None,
) -> dict:
    """Shared dispatch: authorize -> call -> envelope -> audit.

    This is the ONLY place a tool result is assembled, so every tool's
    response has exactly the same shape.
    """
    category = permission_category(tool_name)
    correlation_id = _new_correlation_id()
    started = time.monotonic()
    authorization_result = "OK"
    safe_error_category: Optional[str] = None
    approval_required = False

    try:
        if category != READ_ONLY:
            _authorize(tool_name, category, kwargs)

        data = fn(**kwargs)
        status = "completed"
        success = True
        errors: list[str] = []
        warnings: list[str] = []
        if isinstance(data, dict) and data.get("reused") is True:
            warnings.append(
                "An existing result already satisfied this request; the "
                "underlying operation was not repeated."
            )

    except ToolAuthorizationError as exc:
        authorization_result = "DENIED"
        safe_error_category = exc.code
        status = "blocked"
        success = False
        data = {}
        errors = [f"{exc.code}: {exc.message}"]
        warnings = []
        approval_required = exc.code in (
            "NOT_APPROVED",
            "APPROVAL_NOT_FOUND",
            "PLAN_ID_MISMATCH",
        )

    except OperationInProgressError as exc:
        authorization_result = "DENIED"
        safe_error_category = exc.code
        status = "conflict"
        success = False
        data = {}
        errors = [f"{exc.code}: {exc.message}"]
        warnings = []

    except handlers.NotFoundError as exc:
        authorization_result = "OK"
        safe_error_category = exc.code
        status = "not_found"
        success = False
        data = {}
        errors = [f"{exc.code}: {exc.message}"]
        warnings = []

    except handlers.DeploymentBlockedError as exc:
        authorization_result = "DENIED"
        safe_error_category = exc.code
        status = "blocked"
        success = False
        data = {"deployment_id": exc.deployment_id} if exc.deployment_id else {}
        errors = [f"{exc.code}: {exc.message}"]
        warnings = []
        approval_required = True

    except Exception as exc:  # noqa: BLE001 - deliberately broad, sanitized below
        code, message = map_exception(exc)
        authorization_result = (
            "DENIED" if code not in ("INTERNAL_ERROR",) and category != READ_ONLY else "OK"
        )
        safe_error_category = code
        status = "failed"
        success = False
        data = {}
        errors = [f"{code}: {message}"]
        warnings = []
        approval_required = code in (
            "NOT_APPROVED",
            "APPROVAL_NOT_FOUND",
            "PLAN_ID_MISMATCH",
            "APPROVAL_INVALIDATED",
            "VERSION_MISMATCH",
            "FINGERPRINT_MISMATCH",
        )

    duration_ms = int((time.monotonic() - started) * 1000)

    envelope = build_envelope(
        success=success,
        operation=tool_name,
        status=status,
        correlation_id=correlation_id,
        permission_category=category,
        data=data,
        warnings=warnings,
        errors=errors,
        approval_required=approval_required,
        next_allowed_actions=next_allowed_actions(tool_name) if success else [],
    )

    try:
        record_audit(
            correlation_id=correlation_id,
            tool_name=tool_name,
            permission_category=category,
            raw_input=kwargs,
            referenced_ids=referenced_ids,
            authorization_result=authorization_result,
            result_status=status,
            duration_ms=duration_ms,
            safe_error_category=safe_error_category,
        )
    except Exception:  # pragma: no cover - audit must never break the tool call
        pass

    return envelope


# ── Tool registrations ────────────────────────────────────────────
# Each function is a thin FastMCP tool wrapper; all logic is in
# ``_invoke`` + ``src.mcp_server.handlers``.


@mcp.tool()
def health_status() -> dict:
    """Report basic service health (name, environment). No cloud calls."""
    return _invoke("health_status", handlers.health_status, {})


@mcp.tool()
def capability_status() -> dict:
    """Report redacted configuration + readiness flags. No cloud calls."""
    return _invoke("capability_status", handlers.capability_status, {})


@mcp.tool()
def verify_azure_environment() -> dict:
    """Read-only verification of the configured Azure/ADF environment."""
    return _invoke("verify_azure_environment", handlers.verify_azure_environment, {})


@mcp.tool()
def verify_fabric_environment() -> dict:
    """Read-only verification of the configured Fabric environment."""
    return _invoke("verify_fabric_environment", handlers.verify_fabric_environment, {})


@mcp.tool()
def get_discovery(discovery_id: Optional[int] = None) -> dict:
    """Return a persisted discovery snapshot by id, or the latest one."""
    return _invoke(
        "get_discovery", handlers.get_discovery, {"discovery_id": discovery_id},
        referenced_ids={"discovery_id": discovery_id},
    )


@mcp.tool()
def get_dependencies(discovery_id: Optional[int] = None) -> dict:
    """Return dependency edges + missing references for a discovery snapshot."""
    return _invoke(
        "get_dependencies", handlers.get_dependencies, {"discovery_id": discovery_id},
        referenced_ids={"discovery_id": discovery_id},
    )


@mcp.tool()
def get_assessment(assessment_id: Optional[int] = None) -> dict:
    """Return a persisted compatibility-assessment run by id, or the latest."""
    return _invoke(
        "get_assessment", handlers.get_assessment, {"assessment_id": assessment_id},
        referenced_ids={"assessment_id": assessment_id},
    )


@mcp.tool()
def get_plan(plan_id: Optional[int] = None) -> dict:
    """Return a persisted migration plan's summary by id, or the latest."""
    return _invoke(
        "get_plan", handlers.get_plan, {"plan_id": plan_id},
        referenced_ids={"plan_id": plan_id},
    )


@mcp.tool()
def get_package_summary(plan_id: int) -> dict:
    """Return a bounded summary of a plan's generated artifact package
    (no full generated definitions)."""
    return _invoke(
        "get_package_summary", handlers.get_package_summary, {"plan_id": plan_id},
        referenced_ids={"plan_id": plan_id},
    )


@mcp.tool()
def get_manifest_summary(plan_id: int) -> dict:
    """Return a plan's generated-package manifest (entries + digests)."""
    return _invoke(
        "get_manifest_summary", handlers.get_manifest_summary, {"plan_id": plan_id},
        referenced_ids={"plan_id": plan_id},
    )


@mcp.tool()
def get_approval_status(plan_id: int) -> dict:
    """Return the latest approval for a plan and whether it can deploy."""
    return _invoke(
        "get_approval_status", handlers.get_approval_status, {"plan_id": plan_id},
        referenced_ids={"plan_id": plan_id},
    )


@mcp.tool()
def get_deployment(deployment_id: Optional[int] = None) -> dict:
    """Return a persisted deployment run by id, or the latest one."""
    return _invoke(
        "get_deployment", handlers.get_deployment, {"deployment_id": deployment_id},
        referenced_ids={"deployment_id": deployment_id},
    )


@mcp.tool()
def get_execution(execution_id: int) -> dict:
    """Return a persisted controlled pipeline execution by id."""
    return _invoke(
        "get_execution", handlers.get_execution_tool, {"execution_id": execution_id},
        referenced_ids={"execution_id": execution_id},
    )


@mcp.tool()
def get_structural_validation(validation_id: Optional[int] = None) -> dict:
    """Return a persisted structural validation run by id, or the latest."""
    return _invoke(
        "get_structural_validation",
        handlers.get_structural_validation_tool,
        {"validation_id": validation_id},
        referenced_ids={"validation_id": validation_id},
    )


@mcp.tool()
def get_runtime_validation(
    validation_id: Optional[int] = None, plan_id: Optional[int] = None
) -> dict:
    """Return a persisted runtime-equivalence validation by id, or the
    latest one for a plan."""
    return _invoke(
        "get_runtime_validation",
        handlers.get_runtime_validation_tool,
        {"validation_id": validation_id, "plan_id": plan_id},
        referenced_ids={"validation_id": validation_id, "plan_id": plan_id},
    )


@mcp.tool()
def get_report(validation_id: int, full: bool = False) -> dict:
    """Return a bounded report summary; pass full=true for the complete
    (still redacted) JSON report body."""
    return _invoke(
        "get_report", handlers.get_report_tool,
        {"validation_id": validation_id, "full": full},
        referenced_ids={"validation_id": validation_id},
    )


@mcp.tool()
def get_final_migration_status(plan_id: int) -> dict:
    """Return an aggregate status across discovery/assessment/plan/approval
    /deployment/structural/runtime/report for one plan's lineage."""
    return _invoke(
        "get_final_migration_status",
        handlers.get_final_migration_status,
        {"plan_id": plan_id},
        referenced_ids={"plan_id": plan_id},
    )


# ── State change ───────────────────────────────────────────────────


@mcp.tool()
def scan_adf(mode: str = "fixture") -> dict:
    """Run a discovery scan ('fixture' or 'azure') and persist a new
    snapshot. mode='azure' only runs when Azure discovery is enabled and
    fully configured; it is strictly read-only (GET-only) against Azure."""
    return _invoke("scan_adf", handlers.scan_adf, {"mode": mode})


@mcp.tool()
def run_assessment() -> dict:
    """Assess the latest discovery snapshot and persist a new assessment run."""
    return _invoke("run_assessment", handlers.run_assessment_tool, {})


@mcp.tool()
def generate_plan() -> dict:
    """Generate and persist a new migration plan from the latest discovery
    + assessment."""
    return _invoke("generate_plan", handlers.generate_plan_tool, {})


@mcp.tool()
def request_approval(plan_id: int, requested_by: str, comment: str = "") -> dict:
    """Create a PENDING approval request for a plan.

    This tool can only create/read a persisted approval record through the
    existing approval workflow. There is no corresponding decide/approve
    tool in this server — approval decisions can only be made by a human
    through the existing web approval workflow, so an MCP caller can never
    self-approve a plan."""
    return _invoke(
        "request_approval",
        handlers.request_approval_tool,
        {"plan_id": plan_id, "requested_by": requested_by, "comment": comment},
        referenced_ids={"plan_id": plan_id},
    )


@mcp.tool()
def run_structural_validation(deployment_id: int) -> dict:
    """Run + persist artifact-definition structural validation for a MOCK
    deployment."""
    return _invoke(
        "run_structural_validation",
        handlers.run_structural_validation_tool,
        {"deployment_id": deployment_id},
        referenced_ids={"deployment_id": deployment_id},
    )


@mcp.tool()
def run_runtime_validation(source_execution_id: int, target_execution_id: int) -> dict:
    """Run + persist the optional runtime-equivalence comparison for a
    source/target execution pair. Never alters structural validation status."""
    return _invoke(
        "run_runtime_validation",
        handlers.run_runtime_validation_tool,
        {
            "source_execution_id": source_execution_id,
            "target_execution_id": target_execution_id,
        },
        referenced_ids={
            "source_execution_id": source_execution_id,
            "target_execution_id": target_execution_id,
        },
    )


@mcp.tool()
def generate_report(validation_id: int, force: bool = False) -> dict:
    """Generate (or, if one already exists and force=false, return) the
    migration report for a structural validation run."""
    return _invoke(
        "generate_report",
        handlers.generate_report_tool,
        {"validation_id": validation_id, "force": force},
        referenced_ids={"validation_id": validation_id},
    )


# ── Guarded cloud write ────────────────────────────────────────────


@mcp.tool()
def deploy_fabric_package(plan_id: int, approval_id: int, mode: str) -> dict:
    """Deploy an approved plan's generated package (mode: DRY_RUN, MOCK, or
    REAL). REAL only runs when Fabric deployment is enabled/configured, and
    only ever targets the one configured Fabric workspace — reuses the
    exact same ``validate_deployment_authorization`` / package-verification
    gate as the HTTP API. A duplicate REAL deploy for the same plan+approval
    returns the existing result instead of deploying twice."""
    return _invoke(
        "deploy_fabric_package",
        handlers.deploy_fabric_package_tool,
        {
            "plan_id": plan_id,
            "approval_id": approval_id,
            "mode": mode,
            "correlation_id": _new_correlation_id(),
        },
        referenced_ids={"plan_id": plan_id, "approval_id": approval_id, "mode": mode},
    )


# ── Guarded execution ──────────────────────────────────────────────


@mcp.tool()
def run_source_pipeline(
    plan_id: Optional[int] = None,
    deployment_id: Optional[int] = None,
    discovery_snapshot_id: Optional[int] = None,
    correlation_id: Optional[str] = None,
) -> dict:
    """Run the ONE configured source ADF pipeline (never a caller-supplied
    name) to completion and persist safe run metadata. Disabled unless
    RUNTIME_EXECUTION_ENABLED and Azure/ADF source settings are fully
    configured. Passing the same correlation_id as a prior call returns
    that prior execution instead of starting a duplicate run."""
    correlation_id = correlation_id or _new_correlation_id()
    return _invoke(
        "run_source_pipeline",
        handlers.run_source_pipeline_tool,
        {
            "plan_id": plan_id,
            "deployment_id": deployment_id,
            "discovery_snapshot_id": discovery_snapshot_id,
            "correlation_id": correlation_id,
        },
        referenced_ids={"plan_id": plan_id, "deployment_id": deployment_id},
    )


@mcp.tool()
def run_fabric_pipeline(
    plan_id: int, deployment_id: int, correlation_id: Optional[str] = None
) -> dict:
    """Run the ONE configured target Fabric Data Pipeline item (never a
    caller-supplied item id) after full authorization (enabled runtime
    execution, matching approved+unchanged package, a REAL deployment of
    exactly the configured pipeline item with a matching read-back digest,
    and a passed structural validation). Passing the same correlation_id as
    a prior call returns that prior execution instead of starting a
    duplicate run."""
    correlation_id = correlation_id or _new_correlation_id()
    return _invoke(
        "run_fabric_pipeline",
        handlers.run_fabric_pipeline_tool,
        {"plan_id": plan_id, "deployment_id": deployment_id, "correlation_id": correlation_id},
        referenced_ids={"plan_id": plan_id, "deployment_id": deployment_id},
    )


def main() -> None:
    """STDIO entry point: ``python -m src.mcp_server.server``."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
