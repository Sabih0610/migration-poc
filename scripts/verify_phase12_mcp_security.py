"""Phase 12 MCP server security-boundary verification (fully mocked).

Probes: no tool schema accepts an arbitrary/override resource id
(subscription/resource-group/factory/pipeline-name/workspace/item-id),
no delete/shell/python/filesystem capability exists anywhere, self-
approval is impossible (no approve/reject tool + deploy always re-reads
the persisted approval status), cross-workspace/cross-plan/arbitrary-
pipeline-name attempts are rejected, audit rows never contain secret
values, and a secret scan of a sample of tool responses finds nothing.

Zero real Azure/Fabric calls. Exit 0 = PASS, non-zero = FAIL.
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_helper import TempDatabase
from src.config import get_settings

_FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        _FAILURES.append(label)


def main() -> int:
    print("=== Phase 12 MCP server security verification ===\n")

    with TempDatabase(prefix="verify_phase12_sec_") as db:
        import src.mcp_server.server as server
        import src.mcp_server.handlers as handlers
        from src.mcp_server.permissions import all_tool_names
        from src.mcp_server.security import BoundaryViolationError, assert_no_environment_overrides

        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}

        # ── No arbitrary resource-id parameters anywhere ────────
        forbidden = {
            "subscription_id", "resource_group", "data_factory_name", "factory_name",
            "pipeline_name", "workspace_id", "fabric_workspace_id", "item_id",
            "fabric_item_id", "capacity_id",
        }
        offenders = []
        for name, tool in tools.items():
            props = set((tool.inputSchema or {}).get("properties", {}).keys())
            if props & forbidden:
                offenders.append((name, props & forbidden))
        check("no tool schema exposes an arbitrary resource-id override", not offenders)

        deploy_props = set(tools["deploy_fabric_package"].inputSchema.get("properties", {}))
        check(
            "deploy_fabric_package only accepts plan_id/approval_id/mode",
            deploy_props == {"plan_id", "approval_id", "mode"},
        )
        source_props = set(tools["run_source_pipeline"].inputSchema.get("properties", {}))
        check("run_source_pipeline has no pipeline_name parameter", "pipeline_name" not in source_props)
        target_props = set(tools["run_fabric_pipeline"].inputSchema.get("properties", {}))
        check(
            "run_fabric_pipeline has no item_id/workspace_id parameter",
            "item_id" not in target_props and "workspace_id" not in target_props,
        )

        # ── No delete/shell/python/filesystem capability ─────────
        names = all_tool_names()
        banned_patterns = (
            r"\bdelete\b", r"\bremove\b", r"\bshell\b", r"\beval\b", r"\bexec\b",
            r"\brun_sql\b", r"\bread_file\b", r"\bwrite_file\b", r"\blist_dir\b",
            r"\brun_python\b", r"\brun_command\b",
        )
        bad_names = [
            n for n in names
            if any(re.search(p, n.lower()) for p in banned_patterns)
        ]
        check("no tool name implies delete/shell/python/filesystem capability", not bad_names)

        handler_source = Path(handlers.__file__).read_text(encoding="utf-8")
        server_source = Path(server.__file__).read_text(encoding="utf-8")
        no_eval_exec = (
            "eval(" not in handler_source and "exec(" not in handler_source
            and "eval(" not in server_source and "exec(" not in server_source
            and "subprocess" not in handler_source and "subprocess" not in server_source
            and "os.system" not in handler_source and "os.system" not in server_source
        )
        check("no eval/exec/subprocess/os.system in handlers.py or server.py", no_eval_exec)

        # ── Boundary-enforcement helper (defense in depth) ───────
        from src.config import Settings

        settings = Settings(fabric_workspace_id="ws-real", adf_source_pipeline_name="pl_real")
        boundary_ok = True
        try:
            assert_no_environment_overrides(settings, fabric_workspace_id="ws-attacker")
            boundary_ok = False
        except BoundaryViolationError:
            pass
        try:
            assert_no_environment_overrides(settings, adf_source_pipeline_name="drop_everything")
            boundary_ok = False
        except BoundaryViolationError:
            pass
        check("boundary helper rejects mismatched workspace/pipeline-name overrides", boundary_ok)

        # ── Self-approval prevention ─────────────────────────────
        check("no approve/reject tool is registered", not any(n.startswith(("approve", "reject")) for n in names))
        request_approval_props = set(tools["request_approval"].inputSchema.get("properties", {}))
        check(
            "request_approval accepts no 'approved'/'status'/'decided_by' shortcut field",
            not ({"approved", "status", "decided_by"} & request_approval_props),
        )

        server.scan_adf(mode="fixture")
        server.run_assessment()
        plan = server.generate_plan()
        plan_id = plan["data"]["plan_id"]
        approval = server.request_approval(plan_id=plan_id, requested_by="alice")
        approval_id = approval["data"]["approval_id"]
        check("request_approval always creates a PENDING record", approval["data"]["status"] == "PENDING")

        pending_deploy = server.deploy_fabric_package(plan_id, approval_id, "MOCK")
        check(
            "deploy is blocked while approval is still PENDING (no self-approval possible)",
            pending_deploy["success"] is False and pending_deploy["approval_required"] is True,
        )

        # ── Cross-plan approval reuse rejected ───────────────────
        from src.approvals import approval_service as approval_svc

        approval_svc.approve(approval_id, "bob", "ok")
        server.scan_adf(mode="fixture")
        server.run_assessment()
        plan2 = server.generate_plan()
        plan2_id = plan2["data"]["plan_id"]
        cross_plan = server.deploy_fabric_package(plan2_id, approval_id, "MOCK")
        check(
            "cross-plan approval reuse rejected",
            cross_plan["success"] is False and "PLAN_ID_MISMATCH" in cross_plan["errors"][0],
        )

        # ── Arbitrary pipeline-name rejected at the connector boundary ──
        from tests import execution_helpers as eh
        from src.connectors.azure_adf_executor import AzureExecutionError

        executor, fake_client = eh.make_executor(statuses=["Succeeded"])
        boundary_rejected = False
        try:
            executor.start_run("an_attacker_supplied_pipeline_name")
        except AzureExecutionError as exc:
            boundary_rejected = exc.code == "ADF_EXECUTION_BOUNDARY_VIOLATION"
        check(
            "arbitrary pipeline name is rejected at the executor boundary "
            "(and never reaches the fake Azure SDK client)",
            boundary_rejected and not fake_client.pipelines.calls,
        )

        # ── Audit redaction ──────────────────────────────────────
        os.environ["AZURE_CLIENT_SECRET"] = "SCAN-SECRET-VALUE-42"
        os.environ["FABRIC_CLIENT_SECRET"] = "SCAN-SECRET-VALUE-43"
        get_settings.cache_clear()

        from src.mcp_server.audit_store import list_audit_records

        sample_calls = [
            server.health_status(),
            server.capability_status(),
            server.get_plan(plan_id),
        ]
        secret_in_audit = False
        for result in sample_calls:
            for row in list_audit_records(correlation_id=result["correlation_id"]):
                blob = json.dumps(row)
                if "SCAN-SECRET-VALUE-42" in blob or "SCAN-SECRET-VALUE-43" in blob:
                    secret_in_audit = True
        check("audit rows never contain secret values", not secret_in_audit)

        # ── Secret scan of a sample of tool responses ────────────
        secret_in_response = any(
            "SCAN-SECRET-VALUE-42" in json.dumps(r) or "SCAN-SECRET-VALUE-43" in json.dumps(r)
            for r in sample_calls
        )
        check("sample tool responses never contain secret values", not secret_in_response)

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
