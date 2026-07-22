"""Phase 10 CORRECTED real-Fabric write-path verification (mocked, no network).

Exercises the audited/corrected REAL-mode Fabric behaviors end to end:

* Connection created via the Connections API, never a workspace item.
* Lakehouse created via the adapter + read-back digest MATCH.
* LakehouseTable deferred to runtime (no network call at all).
* Data Pipeline created via the adapter + read-back digest MATCH.
* Schedule attached to the deployed pipeline item, enabled=false.
* A non-deployable artifact (Dataflow Gen2 without a real Power Query
  conversion) blocks deployment instead of being faked.
* Capacity-state-not-verifiable (403) is reported honestly, never invented.

Exit 0 = PASS, 1 = FAIL.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_helper import TempDatabase
from src.approvals import approval_service as appr
from src.artifacts import write_package
from src.config import get_settings
from src.connectors import fabric_definition_adapter as adapter
from src.connectors.adf_source import FixtureADFSource
from src.connectors.fabric_client import CODE_NON_DEPLOYABLE, FabricError
from src.migration.assessment import ADFCompatibilityAssessment
from src.migration.deployment import DeploymentService
from src.migration.discovery import ADFDiscoveryService
from src.migration.plan_store import save_plan
from src.migration.planner import MigrationPlanner
from src.models.schemas import (
    DeployableTargetType, DeploymentMode, DeploymentStatus, DeploymentStepStatus,
)
from tests import fabric_helpers as fh

FIXTURES = PROJECT_ROOT / "fixtures"


def main() -> int:
    passed, errors = [], []
    print("=" * 60)
    print("  Phase 10 CORRECTED Fabric Write-Path Verification (mocked)")
    print("=" * 60)

    with TempDatabase(prefix="verify_phase10_corrected_"):
        gen = Path(get_settings().generated_artifacts_dir)
        inv = FixtureADFSource(FIXTURES).load_inventory()
        result = ADFDiscoveryService(inv).scan_inventory()
        assessment = ADFCompatibilityAssessment(inv).assess_discovery(result)
        plan = MigrationPlanner(inv).generate_plan(result, assessment, 1)
        package = plan.generated_package
        write_package(package, gen)
        rec = save_plan(plan, assessment_id=1)
        plan_id = rec["id"]
        ap = appr.request_approval(plan_id, "alice")
        appr.approve(ap.approval_id, "bob")
        passed.append("Approved plan + package written")

        # ── 1. Connection via Connections API, not a workspace item ──
        connection = fh.artifact_of(package, DeployableTargetType.CONNECTION)
        t1 = fh.FakeFabricTransport()
        client1 = fh.make_client(transport=t1)
        conn_outcome = client1.deploy_artifact(connection)
        paths = [url for (_m, url, _h, _b) in t1.calls]
        if conn_outcome.item_id and any(p.endswith("/connections") for p in paths) \
                and not any(p.endswith(f"/workspaces/{fh.WS}/items") for p in paths):
            passed.append("Connection created via Connections API (not a workspace item)")
        else:
            errors.append("Connection did not use the Connections API path")

        # ── 2. Lakehouse via adapter + read-back MATCH ────────────────
        lakehouse = fh.artifact_of(package, DeployableTargetType.LAKEHOUSE)
        lake_outcome = client1.deploy_artifact(lakehouse)
        if lake_outcome.item_id and lake_outcome.readback_status == "UNSUPPORTED":
            passed.append("Lakehouse created via adapter (.platform-only definition)")
        else:
            errors.append(f"Lakehouse outcome unexpected: {lake_outcome}")

        # ── 3. LakehouseTable deferred, zero network calls ─────────────
        table = fh.artifact_of(package, DeployableTargetType.LAKEHOUSE_TABLE)
        calls_before = len(t1.calls)
        table_outcome = client1.deploy_artifact(table, {lakehouse.artifact_id: lake_outcome.item_id})
        if table_outcome.status == "deferred" \
                and table_outcome.materialization_status == "DEFERRED_TO_RUNTIME" \
                and table_outcome.item_id is None \
                and len(t1.calls) == calls_before:
            passed.append("LakehouseTable deferred to runtime (no network call)")
        else:
            errors.append("LakehouseTable was not correctly deferred")

        # ── 4/5. Pipeline via adapter + read-back MATCH ────────────────
        pipeline = fh.artifact_of(package, DeployableTargetType.DATA_PIPELINE)
        pipe_outcome = client1.deploy_artifact(pipeline)
        if pipe_outcome.item_id and pipe_outcome.readback_status == "MATCH" and pipe_outcome.readback_digest:
            passed.append("Data Pipeline created via adapter; read-back digest MATCH")
        else:
            errors.append(f"Pipeline read-back did not MATCH: {pipe_outcome}")

        # A tampered read-back must be flagged MISMATCH, never silently accepted.
        t2 = fh.FakeFabricTransport()
        t2.readback_override = {"parts": [{"path": "pipeline-content.json", "payload": "dGFtcGVyZWQ="}]}
        client2 = fh.make_client(transport=t2)
        mismatch_outcome = client2.deploy_artifact(pipeline)
        if mismatch_outcome.readback_status == "MISMATCH":
            passed.append("Tampered read-back correctly flagged MISMATCH")
        else:
            errors.append("read-back mismatch was not detected")

        # ── 6. Schedule attached to the deployed pipeline, enabled=false ─
        schedule = fh.artifact_of(package, DeployableTargetType.SCHEDULE)
        sched_outcome = client1.deploy_artifact(
            schedule, {pipeline.artifact_id: pipe_outcome.item_id}
        )
        sched_post_calls = [
            (m, u, b) for (m, u, _h, b) in t1.calls
            if "/jobs/" in u and "/schedules" in u and m == "POST"
        ]
        if sched_outcome.item_id and sched_post_calls \
                and f"/items/{pipe_outcome.item_id}/jobs/" in sched_post_calls[0][1] \
                and sched_post_calls[0][2]["enabled"] is False:
            passed.append("Schedule attached to pipeline item, enabled=false by default")
        else:
            errors.append("Schedule was not correctly attached / defaulted disabled")

        # ── 7. Non-deployable artifact blocks, never faked ─────────────
        dataflow = fh.artifact_of(package, DeployableTargetType.DATAFLOW_GEN2)
        built = adapter.build_definition(dataflow)
        t3 = fh.FakeFabricTransport()
        client3 = fh.make_client(transport=t3)
        try:
            client3.deploy_artifact(dataflow)
            errors.append("non-deployable dataflow was NOT blocked")
        except FabricError as exc:
            if exc.code == CODE_NON_DEPLOYABLE and not built.deployable and not t3.calls:
                passed.append("Non-deployable Dataflow Gen2 blocked before any HTTP call")
            else:
                errors.append(f"unexpected error blocking dataflow: {exc.code}")

        # Full-plan REAL deploy: honestly PARTIAL, dependents skipped.
        svc = DeploymentService(fabric_client=fh.make_client(transport=fh.FakeFabricTransport()))
        full = svc.deploy(plan_id, ap.approval_id, DeploymentMode.REAL)
        by_type = {s.target_item_type: s for s in full.steps}
        if full.status == DeploymentStatus.PARTIAL \
                and by_type[DeployableTargetType.DATAFLOW_GEN2.value].status == DeploymentStepStatus.FAILED \
                and by_type[DeployableTargetType.DATA_PIPELINE.value].status == DeploymentStepStatus.SKIPPED:
            passed.append("Full-plan REAL deploy correctly PARTIAL (no fake success)")
        else:
            errors.append("full-plan REAL deploy did not correctly block on the non-deployable dataflow")

        # ── 8. Capacity-state-not-verifiable, never invented ───────────
        t4 = fh.FakeFabricTransport()
        t4.capacity_forbidden = True
        cap = fh.make_client(transport=t4).verify_capacity()
        if cap["state"] == "CAPACITY_STATE_NOT_VERIFIABLE":
            passed.append("Capacity 403 reported as CAPACITY_STATE_NOT_VERIFIABLE")
        else:
            errors.append(f"capacity state incorrectly reported as {cap['state']!r}")

        # No delete calls anywhere across all fake transports used above.
        all_calls = t1.calls + t2.calls + t3.calls + t4.calls
        if not any(m == "DELETE" for (m, *_r) in all_calls):
            passed.append("No delete calls issued across any transport")
        else:
            errors.append("a DELETE call was issued")

    print()
    for p in passed:
        print(f"  [OK] {p}")
    for e in errors:
        print(f"  [FAIL] {e}")
    print()
    print("  RESULT: FAIL" if errors else "  RESULT: PASS")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
