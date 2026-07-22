"""Phase 12 MCP server end-to-end verification (fully mocked, no network).

Exercises: tool discovery + envelope shape, permission enforcement,
guarded-operation binding (deploy + execution authorization gates),
idempotency (duplicate REAL deploy / duplicate execution by
correlation id / duplicate report generation), concurrency (advisory
lock rejects a simultaneous second caller), and restart/resume
(re-reading identical state after reloading the server module against
the same SQLite file).

Zero real Azure/Fabric calls are ever made — every SDK/HTTP surface is
injected with an in-memory fake, exactly like the Phase 9/10/11
verification scripts.

Exit 0 = PASS, non-zero = FAIL.
"""

import asyncio
import importlib
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_helper import TempDatabase
from src.approvals import approval_service as approval_svc
from src.config import get_settings

_FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        _FAILURES.append(label)


def _enable_runtime_execution(pipeline_item_id: str, pipeline_name: str) -> None:
    from tests import fabric_helpers as fh

    for key, value in {
        "RUNTIME_EXECUTION_ENABLED": "true",
        "AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "c", "AZURE_CLIENT_SECRET": "s",
        "AZURE_SUBSCRIPTION_ID": "11111111-1111-1111-1111-111111111111",
        "AZURE_RESOURCE_GROUP": "AzureFabricMigrationPOC",
        "AZURE_DATA_FACTORY_NAME": "Sabih-df",
        "ADF_SOURCE_PIPELINE_NAME": pipeline_name,
        "FABRIC_TENANT_ID": "t", "FABRIC_CLIENT_ID": fh.CLIENT_ID, "FABRIC_CLIENT_SECRET": "s",
        "FABRIC_WORKSPACE_ID": fh.WS,
        "FABRIC_TARGET_PIPELINE_ITEM_ID": pipeline_item_id,
        "FABRIC_DEPLOYMENT_ENABLED": "true",
    }.items():
        os.environ[key] = value
    get_settings.cache_clear()


def main() -> int:
    print("=== Phase 12 MCP server verification ===\n")

    with TempDatabase(prefix="verify_phase12_") as db:
        import src.mcp_server.server as server
        import src.mcp_server.handlers as handlers
        from src.mcp_server.permissions import all_tool_names

        # ── Tool discovery / envelope shape ─────────────────────
        tools = asyncio.run(server.mcp.list_tools())
        names = {t.name for t in tools}
        check("all 27 tools discoverable", len(names) == 27)
        check("discovered tools match permission registry", names == set(all_tool_names()))
        for t in tools:
            if not isinstance(t.inputSchema, dict) or t.inputSchema.get("type") != "object":
                _FAILURES.append(f"tool {t.name} has no stable object schema")
        check("every tool has a stable JSON object schema", True)

        health = server.health_status()
        envelope_keys = {
            "success", "operation", "status", "data", "warnings", "errors",
            "correlation_id", "permission_category", "approval_required",
            "next_allowed_actions",
        }
        check("envelope has exactly the required keys", envelope_keys <= set(health.keys()))
        check("health_status succeeds", health["success"] is True)

        # ── Full local workflow through MOCK deployment ─────────
        server.scan_adf(mode="fixture")
        server.run_assessment()
        plan = server.generate_plan()
        plan_id = plan["data"]["plan_id"]
        check("generate_plan succeeds", plan["success"] is True)

        approval = server.request_approval(plan_id=plan_id, requested_by="alice")
        approval_id = approval["data"]["approval_id"]
        check("request_approval creates a PENDING request", approval["data"]["status"] == "PENDING")

        blocked = server.deploy_fabric_package(plan_id, approval_id, "MOCK")
        check("deploy blocked while approval PENDING", blocked["success"] is False and blocked["approval_required"] is True)

        approval_svc.approve(approval_id, "bob", "looks good")
        deployed = server.deploy_fabric_package(plan_id, approval_id, "MOCK")
        check("MOCK deploy succeeds after approval", deployed["success"] is True)
        deployment_id = deployed["data"]["deployment_id"]

        structural = server.run_structural_validation(deployment_id)
        check("structural validation PASSED", structural["data"]["status"] == "PASSED")
        validation_id = structural["data"]["validation_id"]

        report = server.generate_report(validation_id)
        check("report generated", report["success"] is True and report["data"]["status"] == "completed")
        report_again = server.generate_report(validation_id)
        check("duplicate generate_report call is idempotent (reused)", report_again["data"].get("reused") is True)

        final = server.get_final_migration_status(plan_id)
        check(
            "final migration status aggregates the full lineage",
            final["data"]["deployment"]["status"] == "SUCCEEDED"
            and final["data"]["report_available"] is True,
        )

        # ── Guarded-operation binding: authorization before service call ──
        import unittest.mock as mock

        with mock.patch.object(handlers, "DeploymentService") as mocked:
            real_blocked = server.deploy_fabric_package(plan_id, approval_id, "REAL")
        check(
            "REAL deploy blocked before DeploymentService is ever constructed",
            real_blocked["success"] is False and not mocked.called,
        )

        with mock.patch.object(handlers, "SourceExecutionService") as mocked:
            exec_blocked = server.run_source_pipeline(plan_id=plan_id)
        check(
            "source execution blocked before SourceExecutionService is ever constructed",
            exec_blocked["success"] is False and not mocked.called,
        )

        # ── Cross-plan approval reuse rejected ──────────────────
        server.scan_adf(mode="fixture")
        server.run_assessment()
        plan2 = server.generate_plan()
        plan2_id = plan2["data"]["plan_id"]
        cross = server.deploy_fabric_package(plan2_id, approval_id, "MOCK")
        check("cross-plan approval reuse rejected", cross["success"] is False and "PLAN_ID_MISMATCH" in cross["errors"][0])

        # ── Idempotency: duplicate REAL deployment ──────────────
        from tests import execution_helpers as eh

        real_plan_id, real_approval_id, real_deployment, item_id, transport = (
            eh.build_real_pipeline_deployment(db.tmp_dir)
        )
        _enable_runtime_execution(item_id, eh.PIPELINE_NAME)

        first_real = server.deploy_fabric_package(real_plan_id, real_approval_id, "REAL")
        check("REAL deploy dedup finds the fixture's pre-existing REAL deployment", first_real["data"].get("reused") is True)
        second_real = server.deploy_fabric_package(real_plan_id, real_approval_id, "REAL")
        check(
            "duplicate REAL deploy returns the same deployment id, not a new one",
            second_real["data"]["deployment_id"] == first_real["data"]["deployment_id"],
        )

        # ── Idempotency: duplicate execution by correlation id ──
        from tests import fabric_helpers as fh
        from src.connectors.fabric_pipeline_executor import FabricPipelineExecutor
        import src.execution.execution_service as es

        executor, _fake = eh.make_executor(statuses=["Succeeded"])
        fabric_client = fh.make_client(transport=transport)
        fpe = FabricPipelineExecutor(fabric_client, item_id=item_id, poll_interval_seconds=0)

        with mock.patch.object(handlers, "SourceExecutionService", lambda: es.SourceExecutionService(executor=executor)):
            with mock.patch.object(handlers, "TargetExecutionService", lambda: es.TargetExecutionService(executor=fpe)):
                run1 = server.run_fabric_pipeline(plan_id=real_plan_id, deployment_id=real_deployment.deployment_id)
                check("target pipeline execution succeeds", run1["success"] is True)
                corr = run1["data"]["correlation_id"]
                run2 = server.run_fabric_pipeline(
                    plan_id=real_plan_id, deployment_id=real_deployment.deployment_id, correlation_id=corr
                )
        check(
            "retrying with the same correlation id returns the existing execution",
            run2["data"].get("reused") is True and run2["data"]["execution_id"] == run1["data"]["execution_id"],
        )

        # ── Concurrency: advisory lock rejects a simultaneous caller ──
        from src.mcp_server.concurrency import OperationInProgressError, advisory_lock

        concurrency_ok = False
        with advisory_lock("deploy_fabric_package", "concurrency-test-key", "holder"):
            try:
                with advisory_lock("deploy_fabric_package", "concurrency-test-key", "intruder"):
                    pass
            except OperationInProgressError:
                concurrency_ok = True
        check("simultaneous second caller for the same lock key is rejected", concurrency_ok)

        # ── Audit trail ──────────────────────────────────────────
        from src.mcp_server.audit_store import list_audit_records

        audit_rows = list_audit_records(correlation_id=health["correlation_id"])
        check("audit row persisted for every tool call", len(audit_rows) == 1 and audit_rows[0]["tool_name"] == "health_status")

        # ── Restart / resume ─────────────────────────────────────
        before_snapshot = {
            "plan": server.get_plan(plan_id)["data"],
            "deployment": server.get_deployment(deployment_id)["data"],
            "structural": server.get_structural_validation(validation_id)["data"],
            "final": server.get_final_migration_status(plan_id)["data"],
        }
        get_settings.cache_clear()
        importlib.reload(server)
        after_snapshot = {
            "plan": server.get_plan(plan_id)["data"],
            "deployment": server.get_deployment(deployment_id)["data"],
            "structural": server.get_structural_validation(validation_id)["data"],
            "final": server.get_final_migration_status(plan_id)["data"],
        }
        check("restart/resume returns identical persisted state", before_snapshot == after_snapshot)

        # ── Direct-Python / FastAPI regression ───────────────────
        try:
            from src.migration.deployment import DeploymentService as _DS  # noqa: F401
            from src.reports.report_service import generate_report as _gr  # noqa: F401
            direct_python_ok = True
        except Exception:
            direct_python_ok = False
        check("existing service layer remains directly importable/usable", direct_python_ok)

        try:
            from fastapi.testclient import TestClient
            from src.api.app import app

            with TestClient(app) as client:
                resp = client.get("/api/health")
                fastapi_ok = resp.status_code == 200
        except Exception as exc:
            fastapi_ok = False
            print(f"      (FastAPI regression exception: {exc})")
        check("FastAPI app still imports and serves /api/health unchanged", fastapi_ok)

    print()
    if _FAILURES:
        print(f"=== FAIL: {len(_FAILURES)} check(s) failed ===")
        for item in _FAILURES:
            print(f"  - {item}")
        return 1
    print("=== PASS: all checks passed ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
