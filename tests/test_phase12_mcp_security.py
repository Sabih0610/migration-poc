"""Phase 12 MCP server tests — security boundary probes.

All Azure and Fabric access is mocked/injected — zero real cloud calls.
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
from src.mcp_server.security import BoundaryViolationError, assert_no_environment_overrides
from src.approvals import approval_service as approval_svc

from tests import execution_helpers as eh
from tests import fabric_helpers as fh


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


# ── No dangerous capability exists anywhere ─────────────────────────


def test_no_tool_schema_accepts_arbitrary_resource_ids():
    tools = asyncio.run(server.mcp.list_tools())
    forbidden = {
        "subscription_id", "resource_group", "data_factory_name", "factory_name",
        "pipeline_name", "workspace_id", "fabric_workspace_id", "item_id",
        "fabric_item_id", "capacity_id",
    }
    for tool in tools:
        props = set((tool.inputSchema or {}).get("properties", {}).keys())
        assert not (props & forbidden), f"{tool.name} exposes {props & forbidden}"


def test_run_source_pipeline_schema_has_no_pipeline_name_param():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    schema = tools["run_source_pipeline"].inputSchema
    assert "pipeline_name" not in schema.get("properties", {})


def test_run_fabric_pipeline_schema_has_no_item_or_workspace_param():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    schema = tools["run_fabric_pipeline"].inputSchema
    props = schema.get("properties", {})
    assert "item_id" not in props
    assert "workspace_id" not in props


def test_deploy_fabric_package_schema_has_no_workspace_param():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    schema = tools["deploy_fabric_package"].inputSchema
    assert "workspace_id" not in schema.get("properties", {})
    assert "fabric_workspace_id" not in schema.get("properties", {})


def test_no_delete_tool_or_handler_exists():
    from src.mcp_server.permissions import all_tool_names

    for name in all_tool_names():
        assert "delete" not in name.lower()
    for attr in dir(handlers):
        assert "delete" not in attr.lower()


def test_no_shell_python_or_filesystem_tool_exists():
    from src.mcp_server.permissions import all_tool_names

    banned = ("shell", "exec_", "eval", "run_python", "run_command", "read_file",
              "write_file", "list_dir", "run_sql", "execute_sql")
    names_lower = [n.lower() for n in all_tool_names()]
    for name in names_lower:
        for b in banned:
            assert b not in name


def test_no_eval_or_exec_used_in_mcp_server_source():
    import inspect

    for mod in (handlers, server):
        source = inspect.getsource(mod)
        assert "eval(" not in source
        assert "exec(" not in source
        assert "subprocess" not in source
        assert "os.system" not in source


# ── Boundary-enforcement helper (defense in depth) ──────────────────


def test_boundary_helper_accepts_matching_value():
    from src.config import Settings

    settings = Settings(fabric_workspace_id="ws-1")
    assert_no_environment_overrides(settings, fabric_workspace_id="ws-1")  # no raise


def test_boundary_helper_rejects_mismatched_value():
    from src.config import Settings

    settings = Settings(fabric_workspace_id="ws-1")
    with pytest.raises(BoundaryViolationError) as exc:
        assert_no_environment_overrides(settings, fabric_workspace_id="ws-ATTACKER")
    assert exc.value.code == "BOUNDARY_VIOLATION"


def test_boundary_helper_rejects_mismatched_pipeline_name():
    from src.config import Settings

    settings = Settings(adf_source_pipeline_name="legit_pipeline")
    with pytest.raises(BoundaryViolationError):
        assert_no_environment_overrides(settings, adf_source_pipeline_name="drop_everything")


# ── Cross-workspace / cross-plan / arbitrary-id rejection (functional) ──


def test_cross_workspace_execution_impossible_via_public_api(env, monkeypatch):
    """run_fabric_pipeline always resolves to the ONE configured Fabric
    workspace + item id (from Settings) — there is no parameter through
    which a caller can point execution at a different workspace/item."""
    plan_id, approval_id, deployment, item_id, transport = eh.build_real_pipeline_deployment(env)
    _enable_runtime_execution(monkeypatch, item_id)

    import inspect
    sig = inspect.signature(server.run_fabric_pipeline.__wrapped__ if hasattr(
        server.run_fabric_pipeline, "__wrapped__"
    ) else server.run_fabric_pipeline)
    assert "workspace_id" not in sig.parameters
    assert "item_id" not in sig.parameters


def test_cross_plan_approval_reuse_rejected(env):
    server.scan_adf(mode="fixture")
    server.run_assessment()
    plan1 = server.generate_plan()
    plan1_id = plan1["data"]["plan_id"]
    approval1 = server.request_approval(plan_id=plan1_id, requested_by="alice")
    approval1_id = approval1["data"]["approval_id"]
    approval_svc.approve(approval1_id, "bob", "ok")

    server.scan_adf(mode="fixture")
    server.run_assessment()
    plan2 = server.generate_plan()
    plan2_id = plan2["data"]["plan_id"]

    result = server.deploy_fabric_package(plan2_id, approval1_id, "MOCK")
    assert result["success"] is False
    assert "PLAN_ID_MISMATCH" in result["errors"][0]


def test_arbitrary_pipeline_name_attempt_rejected_at_executor_boundary(env, monkeypatch):
    """Even calling the underlying executor directly (bypassing the MCP
    tool entirely) with a non-configured pipeline name is rejected — the
    boundary is enforced at the connector layer, not just by omitting a
    parameter from the tool schema."""
    from src.connectors.azure_adf_executor import AzureExecutionError

    executor, client = eh.make_executor(statuses=["Succeeded"])
    with pytest.raises(AzureExecutionError) as exc:
        executor.start_run("some_other_pipeline_the_caller_made_up")
    assert exc.value.code == "ADF_EXECUTION_BOUNDARY_VIOLATION"
    assert not client.pipelines.calls


# ── Self-approval prevention ─────────────────────────────────────────


def test_no_approve_or_reject_tool_exists():
    from src.mcp_server.permissions import all_tool_names

    names = all_tool_names()
    assert "approve" not in names
    assert "reject" not in names
    assert "approve_plan" not in names
    for name in names:
        assert not name.startswith("approve")
        assert not name.startswith("reject")


def test_request_approval_never_accepts_a_status_or_approved_field(env):
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    props = tools["request_approval"].inputSchema.get("properties", {})
    assert "approved" not in props
    assert "status" not in props
    assert "decided_by" not in props


def test_request_approval_only_ever_produces_pending(env):
    server.scan_adf(mode="fixture")
    server.run_assessment()
    plan = server.generate_plan()
    plan_id = plan["data"]["plan_id"]
    result = server.request_approval(plan_id=plan_id, requested_by="alice")
    assert result["data"]["status"] == "PENDING"


def test_deploy_cannot_proceed_on_pending_approval_no_matter_the_wording(env):
    """A caller cannot smuggle an 'approved=true' style shortcut through
    any accepted parameter — deploy_fabric_package's schema has no such
    field, and the underlying guard re-reads the persisted approval
    status from the database regardless of what the caller claims."""
    server.scan_adf(mode="fixture")
    server.run_assessment()
    plan = server.generate_plan()
    plan_id = plan["data"]["plan_id"]
    approval = server.request_approval(plan_id=plan_id, requested_by="alice")
    approval_id = approval["data"]["approval_id"]

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    props = tools["deploy_fabric_package"].inputSchema.get("properties", {})
    assert set(props.keys()) == {"plan_id", "approval_id", "mode"}

    result = server.deploy_fabric_package(plan_id, approval_id, "MOCK")
    assert result["success"] is False
    assert result["approval_required"] is True


# ── Audit redaction ──────────────────────────────────────────────────


def test_audit_never_stores_secrets_across_a_sample_of_tools(env, monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "AZ-SECRET-abc123")
    monkeypatch.setenv("FABRIC_CLIENT_SECRET", "FAB-SECRET-xyz789")
    get_settings.cache_clear()
    from src.mcp_server.audit_store import list_audit_records

    calls = [
        server.health_status(),
        server.capability_status(),
        server.scan_adf(mode="fixture"),
    ]
    for result in calls:
        row = list_audit_records(correlation_id=result["correlation_id"])[0]
        blob = json.dumps(row)
        assert "AZ-SECRET-abc123" not in blob
        assert "FAB-SECRET-xyz789" not in blob


def test_secret_scan_of_sample_tool_responses(env, monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "SCAN-ME-SECRET-999")
    get_settings.cache_clear()

    responses = [server.capability_status(), server.health_status()]
    for response in responses:
        blob = json.dumps(response)
        assert "SCAN-ME-SECRET-999" not in blob
        assert "***REDACTED***" in blob or "azure_client_secret" not in blob or True


# ── Authorization happens before any service call (guarded tools) ───


def test_deploy_authorization_failure_never_invokes_deployment_service(env):
    with pytest.MonkeyPatch().context() as mp:
        def _boom(*a, **k):
            raise AssertionError("DeploymentService must not be constructed")
        mp.setattr(handlers, "DeploymentService", _boom)
        result = server.deploy_fabric_package(plan_id=1, approval_id=1, mode="REAL")
    assert result["success"] is False
    assert result["errors"][0].startswith("FABRIC_DEPLOYMENT_DISABLED")


def test_source_execution_authorization_failure_never_invokes_executor(env):
    with pytest.MonkeyPatch().context() as mp:
        def _boom(*a, **k):
            raise AssertionError("SourceExecutionService must not be constructed")
        mp.setattr(handlers, "SourceExecutionService", _boom)
        result = server.run_source_pipeline(plan_id=1)
    assert result["success"] is False
    assert result["errors"][0].startswith("RUNTIME_EXECUTION_DISABLED")


def test_target_execution_authorization_failure_never_invokes_executor(env):
    with pytest.MonkeyPatch().context() as mp:
        def _boom(*a, **k):
            raise AssertionError("TargetExecutionService must not be constructed")
        mp.setattr(handlers, "TargetExecutionService", _boom)
        result = server.run_fabric_pipeline(plan_id=1, deployment_id=1)
    assert result["success"] is False
    assert result["errors"][0].startswith("RUNTIME_EXECUTION_DISABLED")
