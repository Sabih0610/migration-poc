"""Single authoritative permission-category registry for every MCP tool.

Classification is by *actual effect*, not by name:

* READ_ONLY — queries persisted state or performs a read-only
  verification call; never writes a new row and never mutates cloud
  state.
* STATE_CHANGE — performs a real, deterministic, *local* (non-cloud)
  service operation that writes a new persisted record (discovery scan,
  assessment, plan, approval request, structural/runtime validation,
  report generation).
* GUARDED_CLOUD_WRITE — may create/modify a real Fabric resource.
* GUARDED_EXECUTION — may trigger a real pipeline run in ADF or Fabric.

Every tool appears in exactly one bucket below. This is the only place
in the codebase that makes this classification.
"""

from __future__ import annotations

READ_ONLY = "READ_ONLY"
STATE_CHANGE = "STATE_CHANGE"
GUARDED_CLOUD_WRITE = "GUARDED_CLOUD_WRITE"
GUARDED_EXECUTION = "GUARDED_EXECUTION"

ALL_CATEGORIES = (READ_ONLY, STATE_CHANGE, GUARDED_CLOUD_WRITE, GUARDED_EXECUTION)

TOOL_PERMISSIONS: dict[str, str] = {
    # ── Read-only ────────────────────────────────────────────────
    "health_status": READ_ONLY,
    "capability_status": READ_ONLY,
    "verify_azure_environment": READ_ONLY,
    "verify_fabric_environment": READ_ONLY,
    "get_discovery": READ_ONLY,
    "get_dependencies": READ_ONLY,
    "get_assessment": READ_ONLY,
    "get_plan": READ_ONLY,
    "get_package_summary": READ_ONLY,
    "get_manifest_summary": READ_ONLY,
    "get_approval_status": READ_ONLY,
    "get_deployment": READ_ONLY,
    "get_execution": READ_ONLY,
    "get_structural_validation": READ_ONLY,
    "get_runtime_validation": READ_ONLY,
    "get_report": READ_ONLY,
    "get_final_migration_status": READ_ONLY,
    # ── State change (real, local, non-cloud; writes a new record) ──
    # NOTE: scan_adf, run_assessment, and generate_plan each persist a
    # new snapshot/run/plan row, so — per "classify by actual effect,
    # not by name" — they are STATE_CHANGE, not READ_ONLY, even though
    # the source ADF calls they may trigger (when mode="azure") are
    # themselves strictly read-only (GET-only) against Azure.
    "scan_adf": STATE_CHANGE,
    "run_assessment": STATE_CHANGE,
    "generate_plan": STATE_CHANGE,
    "request_approval": STATE_CHANGE,
    "run_structural_validation": STATE_CHANGE,
    "run_runtime_validation": STATE_CHANGE,
    "generate_report": STATE_CHANGE,
    # ── Guarded cloud write ──────────────────────────────────────
    "deploy_fabric_package": GUARDED_CLOUD_WRITE,
    # ── Guarded execution ────────────────────────────────────────
    "run_source_pipeline": GUARDED_EXECUTION,
    "run_fabric_pipeline": GUARDED_EXECUTION,
}

# Tool names that require idempotency/concurrency protection (Phase 12 §G).
IDEMPOTENT_GUARDED_TOOLS = {
    "deploy_fabric_package",
    "run_source_pipeline",
    "run_fabric_pipeline",
    "generate_report",
}

# A sensible "what to call next" map used to populate next_allowed_actions.
# Deliberately conservative — it never implies delete/shell/filesystem tools
# because none exist.
_NEXT_ACTIONS: dict[str, list[str]] = {
    "health_status": ["capability_status", "scan_adf"],
    "capability_status": ["scan_adf", "verify_azure_environment", "verify_fabric_environment"],
    "verify_azure_environment": ["scan_adf"],
    "verify_fabric_environment": ["deploy_fabric_package"],
    "scan_adf": ["get_discovery", "get_dependencies", "run_assessment"],
    "get_discovery": ["get_dependencies", "run_assessment"],
    "get_dependencies": ["run_assessment"],
    "run_assessment": ["get_assessment", "generate_plan"],
    "get_assessment": ["generate_plan"],
    "generate_plan": ["get_plan", "get_package_summary", "request_approval"],
    "get_plan": ["get_package_summary", "get_manifest_summary", "request_approval"],
    "get_package_summary": ["request_approval"],
    "get_manifest_summary": ["request_approval"],
    "request_approval": ["get_approval_status"],
    "get_approval_status": ["deploy_fabric_package"],
    "deploy_fabric_package": ["get_deployment", "run_structural_validation"],
    "get_deployment": ["run_structural_validation"],
    "run_structural_validation": ["get_structural_validation", "generate_report"],
    "get_structural_validation": ["generate_report", "run_runtime_validation"],
    "run_runtime_validation": ["get_runtime_validation"],
    "get_runtime_validation": ["generate_report"],
    "generate_report": ["get_report"],
    "get_report": ["get_final_migration_status"],
    "get_execution": ["get_final_migration_status"],
    "run_source_pipeline": ["get_execution"],
    "run_fabric_pipeline": ["get_execution", "run_runtime_validation"],
    "get_final_migration_status": [],
}


def permission_category(tool_name: str) -> str:
    """Return the permission category for a tool, raising on unknown tools
    (every registered tool MUST be classified)."""
    try:
        return TOOL_PERMISSIONS[tool_name]
    except KeyError as exc:
        raise KeyError(f"Tool '{tool_name}' has no permission classification.") from exc


def next_allowed_actions(tool_name: str) -> list[str]:
    return list(_NEXT_ACTIONS.get(tool_name, []))


def all_tool_names() -> list[str]:
    return sorted(TOOL_PERMISSIONS)
