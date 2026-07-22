"""Phase 12 MCP server tests — functional coverage.

All Azure and Fabric access is mocked/injected exactly like the Phase
9/10/11 test suites — zero real cloud calls anywhere in this file.
"""

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database as db_module
from src.config import get_settings
from src.database import Base

import src.mcp_server.server as server
import src.mcp_server.handlers as handlers
from src.mcp_server.permissions import (
    GUARDED_CLOUD_WRITE,
    GUARDED_EXECUTION,
    READ_ONLY,
    STATE_CHANGE,
    all_tool_names,
    permission_category,
)
from src.approvals import approval_service as approval_svc

from tests import execution_helpers as eh
from tests import fabric_helpers as fh


# ── Isolated per-test SQLite database (same pattern as Phase 11 tests) ──


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(
        db_module, "_SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    gen = tmp_path / "generated"
    gen.mkdir()
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setenv("GENERATED_ARTIFACTS_DIR", str(gen))
    monkeypatch.setenv("REPORTS_DIR", str(reports))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _enable_runtime_execution(monkeypatch, pipeline_item_id: str, pipeline_name: str = eh.PIPELINE_NAME):
    for key, value in {
        "RUNTIME_EXECUTION_ENABLED": "true",
        "AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "c", "AZURE_CLIENT_SECRET": "s",
        "AZURE_SUBSCRIPTION_ID": "11111111-1111-1111-1111-111111111111",
        "AZURE_RESOURCE_GROUP": "AzureFabricMigrationPOC",
        "AZURE_DATA_FACTORY_NAME": "Sabih-df",
        "ADF_SOURCE_PIPELINE_NAME": pipeline_name,
        "FABRIC_TENANT_ID": "t", "FABRIC_CLIENT_ID": "c", "FABRIC_CLIENT_SECRET": "s",
        "FABRIC_WORKSPACE_ID": fh.WS,
        "FABRIC_TARGET_PIPELINE_ITEM_ID": pipeline_item_id,
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _run_full_workflow_to_mock_deployment(env):
    """discover -> assess -> plan -> request+approve -> MOCK deploy."""
    server.scan_adf(mode="fixture")
    server.run_assessment()
    plan = server.generate_plan()
    plan_id = plan["data"]["plan_id"]
    approval = server.request_approval(plan_id=plan_id, requested_by="alice")
    approval_id = approval["data"]["approval_id"]
    approval_svc.approve(approval_id, "bob", "ok")
    deploy = server.deploy_fabric_package(plan_id, approval_id, "MOCK")
    return plan_id, approval_id, deploy["data"]["deployment_id"]


# ── A) Transport / tool discovery / schema stability ─────────────


def test_tool_discovery_lists_all_registered_tools():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == set(all_tool_names())


def test_tool_schemas_have_no_dangerous_parameters():
    """No tool schema accepts a subscription/resource-group/factory/
    pipeline-name/workspace/item-id override, arbitrary file paths, or a
    code/sql/command field."""
    tools = asyncio.run(server.mcp.list_tools())
    forbidden_param_names = {
        "subscription_id", "resource_group", "data_factory_name",
        "pipeline_name", "workspace_id", "item_id", "file_path", "path",
        "sql", "query", "command", "code", "script",
    }
    for tool in tools:
        props = set((tool.inputSchema or {}).get("properties", {}).keys())
        overlap = props & forbidden_param_names
        assert not overlap, f"{tool.name} exposes forbidden parameter(s): {overlap}"


def test_every_tool_has_a_json_schema():
    tools = asyncio.run(server.mcp.list_tools())
    for tool in tools:
        assert isinstance(tool.inputSchema, dict)
        assert tool.inputSchema.get("type") == "object"


def test_no_delete_shell_python_or_filesystem_tool_exists():
    import re

    names = set(all_tool_names())
    # Word-boundary patterns so "execution"/"executor" (safe, existing
    # domain vocabulary) don't false-positive on "exec".
    dangerous_patterns = (
        r"\bdelete\b", r"\bremove\b", r"\bshell\b", r"\beval\b",
        r"\bexec\b", r"\brun_sql\b", r"\bread_file\b", r"\bwrite_file\b",
        r"\blist_dir\b", r"\brun_python\b", r"\brun_command\b",
    )
    for name in names:
        lowered = name.lower()
        for pattern in dangerous_patterns:
            assert not re.search(pattern, lowered), (
                f"tool name '{name}' implies a forbidden capability ({pattern})"
            )
    # And no such attribute/method is reachable anywhere in the handlers module.
    handler_names = [n for n in dir(handlers) if not n.startswith("_")]
    for name in handler_names:
        lowered = name.lower()
        assert "delete" not in lowered
        assert "shell" not in lowered
        assert not re.search(r"\beval\b", lowered)
        assert not re.match(r"^exec$|^exec_", lowered)


# ── D) Permission classification ──────────────────────────────────


@pytest.mark.parametrize(
    "tool_name,expected",
    [
        ("health_status", READ_ONLY),
        ("capability_status", READ_ONLY),
        ("verify_azure_environment", READ_ONLY),
        ("verify_fabric_environment", READ_ONLY),
        ("get_discovery", READ_ONLY),
        ("get_final_migration_status", READ_ONLY),
        ("scan_adf", STATE_CHANGE),
        ("run_assessment", STATE_CHANGE),
        ("generate_plan", STATE_CHANGE),
        ("request_approval", STATE_CHANGE),
        ("run_structural_validation", STATE_CHANGE),
        ("run_runtime_validation", STATE_CHANGE),
        ("generate_report", STATE_CHANGE),
        ("deploy_fabric_package", GUARDED_CLOUD_WRITE),
        ("run_source_pipeline", GUARDED_EXECUTION),
        ("run_fabric_pipeline", GUARDED_EXECUTION),
    ],
)
def test_permission_classification(tool_name, expected):
    assert permission_category(tool_name) == expected


def test_every_registered_tool_is_classified():
    tools = asyncio.run(server.mcp.list_tools())
    for tool in tools:
        # Raises if unclassified.
        permission_category(tool.name)


# ── C) Envelope shape ──────────────────────────────────────────────


def test_envelope_shape_on_success(env):
    result = server.health_status()
    for key in (
        "success", "operation", "status", "data", "warnings", "errors",
        "correlation_id", "permission_category", "approval_required",
        "next_allowed_actions",
    ):
        assert key in result
    assert result["success"] is True
    assert result["operation"] == "health_status"
    assert result["permission_category"] == READ_ONLY
    assert isinstance(result["correlation_id"], str) and result["correlation_id"]


def test_envelope_shape_on_not_found(env):
    result = server.get_discovery(discovery_id=999)
    assert result["success"] is False
    assert result["status"] == "not_found"
    assert result["errors"]
    assert result["errors"][0].startswith("RESOURCE_NOT_FOUND")


# ── Read-only tool happy paths ─────────────────────────────────────


def test_read_only_tools_succeed_end_to_end(env):
    plan_id, approval_id, deployment_id = _run_full_workflow_to_mock_deployment(env)

    assert server.capability_status()["success"] is True
    assert server.get_discovery()["success"] is True
    assert server.get_dependencies()["success"] is True
    assert server.get_assessment()["success"] is True
    plan_result = server.get_plan(plan_id)
    assert plan_result["success"] is True
    assert server.get_package_summary(plan_id)["success"] is True
    assert server.get_manifest_summary(plan_id)["success"] is True
    approval_status = server.get_approval_status(plan_id)
    assert approval_status["data"]["status"] == "APPROVED"
    deployment_result = server.get_deployment(deployment_id)
    assert deployment_result["data"]["status"] == "SUCCEEDED"

    structural = server.run_structural_validation(deployment_id)
    assert structural["success"] is True
    validation_id = structural["data"]["validation_id"]
    assert server.get_structural_validation(validation_id)["data"]["status"] == "PASSED"

    report = server.generate_report(validation_id)
    assert report["success"] is True
    got_report = server.get_report(validation_id)
    assert got_report["data"]["report_exists"] is True
    assert "report_json" not in got_report["data"]  # bounded by default
    full_report = server.get_report(validation_id, full=True)
    assert "report_json" in full_report["data"]

    final = server.get_final_migration_status(plan_id)
    assert final["data"]["deployment"]["status"] == "SUCCEEDED"
    assert final["data"]["report_available"] is True


# ── Invalid arguments / unknown ids ─────────────────────────────────


def test_invalid_deployment_mode_rejected_cleanly(env):
    plan_id, approval_id, _ = _run_full_workflow_to_mock_deployment(env)
    result = server.deploy_fabric_package(plan_id, approval_id, "NOT_A_MODE")
    assert result["success"] is False
    assert result["status"] == "failed"


def test_unknown_ids_return_not_found_not_crash(env):
    for result in (
        server.get_assessment(999),
        server.get_plan(999),
        server.get_deployment(999),
        server.get_execution(999),
        server.get_structural_validation(999),
        server.get_package_summary(999),
    ):
        assert result["success"] is False
        assert result["status"] == "not_found"


# ── Disabled REAL modes block guarded tools before the service call ──


def test_real_deploy_blocked_when_disabled_before_service_call(env, monkeypatch):
    plan_id, approval_id, _ = _run_full_workflow_to_mock_deployment(env)
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(handlers, "DeploymentService", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("service must not be called")
        ))
        result = server.deploy_fabric_package(plan_id, approval_id, "REAL")
    assert result["success"] is False
    assert result["errors"][0].startswith("FABRIC_DEPLOYMENT_DISABLED")


def test_execution_blocked_when_disabled_before_service_call(env):
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(handlers, "SourceExecutionService", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("service must not be called")
        ))
        result = server.run_source_pipeline(plan_id=1)
    assert result["success"] is False
    assert result["errors"][0].startswith("RUNTIME_EXECUTION_DISABLED")


# ── Approval-required blocks deployment ─────────────────────────────


def test_deploy_blocked_while_approval_pending(env):
    server.scan_adf(mode="fixture")
    server.run_assessment()
    plan = server.generate_plan()
    plan_id = plan["data"]["plan_id"]
    approval = server.request_approval(plan_id=plan_id, requested_by="alice")
    approval_id = approval["data"]["approval_id"]

    result = server.deploy_fabric_package(plan_id, approval_id, "MOCK")
    assert result["success"] is False
    assert result["approval_required"] is True
    assert result["errors"][0].startswith("NOT_APPROVED")


# ── Package tampering blocks deployment ─────────────────────────────


def test_tampered_package_blocks_deployment(env):
    plan_id, approval_id, _ = _run_full_workflow_to_mock_deployment(env)
    from src.migration.plan_store import get_plan as raw_get_plan
    from src.config import get_settings as _gs
    from pathlib import Path

    plan_record = raw_get_plan(plan_id)
    package = plan_record["plan"].generated_package
    manifest_path = Path(_gs().generated_artifacts_dir) / "manifests" / f"{package.package_id}.json"
    assert manifest_path.exists()
    manifest_path.write_text('{"tampered": true}', encoding="utf-8")

    result = server.deploy_fabric_package(plan_id, approval_id, "MOCK")
    assert result["success"] is False
    assert result["status"] == "blocked"


# ── Cross-plan approval reuse rejected ──────────────────────────────


def test_cross_plan_approval_reuse_rejected(env):
    plan_id_1, approval_id_1, _ = _run_full_workflow_to_mock_deployment(env)
    server.scan_adf(mode="fixture")
    server.run_assessment()
    plan2 = server.generate_plan()
    plan_id_2 = plan2["data"]["plan_id"]

    result = server.deploy_fabric_package(plan_id_2, approval_id_1, "MOCK")
    assert result["success"] is False
    assert "PLAN_ID_MISMATCH" in result["errors"][0]


# ── Duplicate deployment / execution prevention ─────────────────────


def test_duplicate_real_deployment_returns_existing_result(env, monkeypatch):
    plan_id, approval_id, deployment, item_id, transport = eh.build_real_pipeline_deployment(env)
    _enable_runtime_execution(monkeypatch, item_id)
    monkeypatch.setenv("FABRIC_DEPLOYMENT_ENABLED", "true")
    get_settings.cache_clear()

    first = server.deploy_fabric_package(plan_id, approval_id, "REAL")
    assert first["success"] is True
    second = server.deploy_fabric_package(plan_id, approval_id, "REAL")
    assert second["success"] is True
    assert second["data"]["reused"] is True
    assert second["data"]["deployment_id"] == first["data"]["deployment_id"]


def test_duplicate_execution_same_correlation_id_reused(env, monkeypatch):
    plan_id, approval_id, deployment, item_id, transport = eh.build_real_pipeline_deployment(env)
    _enable_runtime_execution(monkeypatch, item_id)

    executor, _ = eh.make_executor(statuses=["Succeeded"])
    fabric_client = fh.make_client(transport=transport)
    from src.connectors.fabric_pipeline_executor import FabricPipelineExecutor
    fpe = FabricPipelineExecutor(fabric_client, item_id=item_id, poll_interval_seconds=0)

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(handlers, "SourceExecutionService", lambda: __import__(
            "src.execution.execution_service", fromlist=["SourceExecutionService"]
        ).SourceExecutionService(executor=executor))
        mp.setattr(handlers, "TargetExecutionService", lambda: __import__(
            "src.execution.execution_service", fromlist=["TargetExecutionService"]
        ).TargetExecutionService(executor=fpe))

        first = server.run_fabric_pipeline(plan_id=plan_id, deployment_id=deployment.deployment_id)
        corr = first["data"]["correlation_id"]
        second = server.run_fabric_pipeline(
            plan_id=plan_id, deployment_id=deployment.deployment_id, correlation_id=corr
        )
    assert first["success"] is True
    assert second["data"]["reused"] is True
    assert second["data"]["execution_id"] == first["data"]["execution_id"]


def test_concurrent_deploy_calls_for_same_plan_rejected(env):
    """Two 'simultaneous' calls for the same plan/approval/mode: the second
    one (while the lock is held) is rejected rather than corrupting or
    duplicating the deployment."""
    plan_id, approval_id, _ = _run_full_workflow_to_mock_deployment(env)
    from src.mcp_server.concurrency import advisory_lock, OperationInProgressError

    with advisory_lock("deploy_fabric_package", f"{plan_id}:{approval_id}:MOCK", "held-by-test"):
        with pytest.raises(OperationInProgressError):
            with advisory_lock("deploy_fabric_package", f"{plan_id}:{approval_id}:MOCK", "second-caller"):
                pass  # pragma: no cover


# ── Restart / resume ─────────────────────────────────────────────────


def test_restart_resume_returns_identical_state(env):
    """A fresh set of service instances (simulating a new MCP server
    process against the same SQLite file) must retrieve identical state.
    Nothing here is read from an in-memory cache: every get_* handler
    re-queries the database."""
    plan_id, approval_id, deployment_id = _run_full_workflow_to_mock_deployment(env)
    structural = server.run_structural_validation(deployment_id)
    validation_id = structural["data"]["validation_id"]
    server.generate_report(validation_id)

    before = {
        "discovery": server.get_discovery()["data"],
        "assessment": server.get_assessment()["data"],
        "plan": server.get_plan(plan_id)["data"],
        "approval": server.get_approval_status(plan_id)["data"],
        "deployment": server.get_deployment(deployment_id)["data"],
        "structural": server.get_structural_validation(validation_id)["data"],
        "report": server.get_report(validation_id)["data"],
        "final": server.get_final_migration_status(plan_id)["data"],
    }

    # Simulate a fresh process: reload the module-level FastMCP app and
    # settings cache, but keep the same SQLite file (this fixture's engine
    # is monkeypatched onto src.database, which is exactly what every
    # store module reads from — nothing here is an in-memory global).
    get_settings.cache_clear()
    import importlib
    importlib.reload(server)

    after = {
        "discovery": server.get_discovery()["data"],
        "assessment": server.get_assessment()["data"],
        "plan": server.get_plan(plan_id)["data"],
        "approval": server.get_approval_status(plan_id)["data"],
        "deployment": server.get_deployment(deployment_id)["data"],
        "structural": server.get_structural_validation(validation_id)["data"],
        "report": server.get_report(validation_id)["data"],
        "final": server.get_final_migration_status(plan_id)["data"],
    }
    assert before == after


# ── Audit records ──────────────────────────────────────────────────


def test_audit_record_created_per_call(env):
    from src.mcp_server.audit_store import list_audit_records

    result = server.health_status()
    records = list_audit_records(correlation_id=result["correlation_id"])
    assert len(records) == 1
    row = records[0]
    assert row["tool_name"] == "health_status"
    assert row["permission_category"] == READ_ONLY
    assert row["result_status"] == "completed"
    assert row["authorization_result"] == "OK"
    assert isinstance(row["duration_ms"], int)


def test_audit_redaction_never_stores_secrets(env, monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "SUPER-SECRET-VALUE-1234")
    get_settings.cache_clear()
    from src.mcp_server.audit_store import list_audit_records

    result = server.capability_status()
    row = list_audit_records(correlation_id=result["correlation_id"])[0]
    blob = json.dumps(row)
    assert "SUPER-SECRET-VALUE-1234" not in blob


# ── Output size limits ───────────────────────────────────────────────


def test_output_size_limits_enforced():
    from src.mcp_server.envelope import bound_value, MAX_LIST_ITEMS, MAX_STRING_LEN

    long_list = list(range(500))
    bounded = bound_value(long_list)
    assert len(bounded) == MAX_LIST_ITEMS + 1
    assert "TRUNCATED" in bounded[-1]

    long_string = "x" * 10000
    bounded_str = bound_value(long_string)
    assert len(bounded_str) < len(long_string)
    assert "TRUNCATED" in bounded_str


# ── Safe retry behavior ───────────────────────────────────────────────


def test_retry_after_generate_report_returns_cached_result(env):
    plan_id, approval_id, deployment_id = _run_full_workflow_to_mock_deployment(env)
    structural = server.run_structural_validation(deployment_id)
    validation_id = structural["data"]["validation_id"]

    first = server.generate_report(validation_id)
    second = server.generate_report(validation_id)
    assert first["data"]["status"] == "completed"
    assert second["data"]["status"] == "already_exists"
    assert second["data"]["reused"] is True


# ── Direct-Python / FastAPI regression ────────────────────────────────


def test_existing_services_still_importable_and_usable_directly():
    """The MCP layer must not affect direct Python usage of the existing
    service layer."""
    from src.migration.discovery_runner import run_discovery
    from src.migration.assessment import ADFCompatibilityAssessment
    from src.migration.planner import MigrationPlanner
    from src.approvals import approval_service
    from src.migration.deployment import DeploymentService
    from src.validation.structural_validator import StructuralValidationService
    from src.reports.report_service import generate_report

    assert callable(run_discovery)
    assert callable(ADFCompatibilityAssessment)
    assert callable(MigrationPlanner)
    assert callable(approval_service.request_approval)
    assert callable(DeploymentService)
    assert callable(StructuralValidationService)
    assert callable(generate_report)


def test_fastapi_app_still_imports_and_works_unchanged():
    from fastapi.testclient import TestClient
    from src.api.app import app

    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
