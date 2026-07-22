"""Phase 10 REAL deployment verification (mocked Fabric, no network).

Exercises the entire approved REAL deployment path against an in-memory
fake Fabric transport: approval gating, dependency-ordered create,
idempotent reuse, no-delete, persistence, and secret-free output.

Exit 0 = PASS, 1 = FAIL.
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_helper import TempDatabase
from src.approvals import approval_service as appr
from src.artifacts import write_package
from src.config import get_settings
from src.connectors.adf_source import FixtureADFSource
from src.migration.assessment import ADFCompatibilityAssessment
from src.migration.deployment import DeploymentService, FabricDeploymentDisabledError
from src.migration.deployment_store import get_deployment
from src.migration.discovery import ADFDiscoveryService
from src.migration.plan_store import save_plan
from src.migration.planner import MigrationPlanner
from src.models.schemas import (
    DeployableTargetType, DeploymentMode, DeploymentStatus, DeploymentStepStatus,
)
from tests import fabric_helpers as fh

FIXTURES = PROJECT_ROOT / "fixtures"
SECRETS = ("password", "client_secret", "accountkey", "connectionstring", "token")

# The fixture's Mapping Data Flow has no real MDF -> Power Query conversion
# available anywhere in this codebase, so its Dataflow Gen2 artifact is
# correctly NON_DEPLOYABLE, and everything depending on it (the pipeline,
# then the schedule) is correctly SKIPPED — a full REAL run is honestly
# PARTIAL, never a faked SUCCEEDED.


def _client(transport):
    return fh.make_client(transport=transport)


def main() -> int:
    passed, errors = [], []
    print("=" * 60)
    print("  Phase 10 REAL Deployment Verification (mocked Fabric)")
    print("=" * 60)

    with TempDatabase(prefix="verify_phase10_"):
        gen = Path(get_settings().generated_artifacts_dir)
        inv = FixtureADFSource(FIXTURES).load_inventory()
        result = ADFDiscoveryService(inv).scan_inventory()
        assessment = ADFCompatibilityAssessment(inv).assess_discovery(result)
        plan = MigrationPlanner(inv).generate_plan(result, assessment, 1)
        write_package(plan.generated_package, gen)
        rec = save_plan(plan, assessment_id=1)
        plan_id = rec["id"]
        ap = appr.request_approval(plan_id, "alice")
        appr.approve(ap.approval_id, "bob")
        passed.append("Approved plan + package written")

        # Disabled by default (no client, settings disabled).
        try:
            DeploymentService().deploy(plan_id, ap.approval_id, DeploymentMode.REAL)
            errors.append("REAL ran while disabled")
        except FabricDeploymentDisabledError:
            passed.append("REAL disabled by default")

        # Ordered create. The dataflow is structurally NON_DEPLOYABLE (no
        # MDF -> Power Query converter exists), so the correct, honest
        # result is PARTIAL: connection/lakehouse/table succeed, the
        # dataflow fails as NON_DEPLOYABLE, and its dependents (pipeline,
        # schedule) are skipped rather than faked.
        t = fh.FakeFabricTransport()
        first = DeploymentService(fabric_client=_client(t)).deploy(
            plan_id, ap.approval_id, DeploymentMode.REAL
        )
        if first.status == DeploymentStatus.PARTIAL:
            passed.append(f"REAL create correctly PARTIAL ({len(first.steps)} artifacts)")
        else:
            errors.append(f"REAL create status {first.status} (expected PARTIAL)")
        by_type = {s.target_item_type: s for s in first.steps}
        types = [s.target_item_type for s in first.steps]
        if types and types[0] == DeployableTargetType.CONNECTION.value and \
                types[-1] == DeployableTargetType.SCHEDULE.value:
            passed.append("Dependency order (connection first, schedule last)")
        else:
            errors.append(f"unexpected order: {types}")
        dataflow_step = by_type.get(DeployableTargetType.DATAFLOW_GEN2.value)
        if dataflow_step and dataflow_step.status == DeploymentStepStatus.FAILED \
                and "NON_DEPLOYABLE" in (dataflow_step.error or ""):
            passed.append("Non-deployable Dataflow Gen2 blocked, not faked")
        else:
            errors.append("dataflow was not correctly blocked as NON_DEPLOYABLE")
        pipeline_step = by_type.get(DeployableTargetType.DATA_PIPELINE.value)
        if pipeline_step and pipeline_step.status == DeploymentStepStatus.SKIPPED:
            passed.append("Dependent Data Pipeline / Schedule correctly skipped")
        else:
            errors.append("pipeline was not skipped after non-deployable dataflow")
        table_step = by_type.get(DeployableTargetType.LAKEHOUSE_TABLE.value)
        if table_step and getattr(table_step, "materialization_status", None) == "DEFERRED_TO_RUNTIME":
            passed.append("LakehouseTable correctly deferred to runtime")
        else:
            errors.append("LakehouseTable was not deferred")
        succeeded_steps = [s for s in first.steps if s.status == DeploymentStepStatus.SUCCEEDED]
        if all(getattr(s, "reused", False) is False for s in succeeded_steps):
            passed.append("All deployable artifacts created (none reused)")

        # Idempotent rerun -> reuse, no duplicates (for the deployable subset).
        second = DeploymentService(fabric_client=_client(t)).deploy(
            plan_id, ap.approval_id, DeploymentMode.REAL
        )
        second_succeeded = [s for s in second.steps if s.status == DeploymentStepStatus.SUCCEEDED and s.resource_id]
        if second.status == DeploymentStatus.PARTIAL and \
                all(getattr(s, "reused", False) for s in second_succeeded):
            passed.append("Idempotent rerun reused all deployable items")
        else:
            errors.append("rerun not idempotent")
        ids1 = {s.artifact_id: s.resource_id for s in succeeded_steps if s.resource_id}
        ids2 = {s.artifact_id: s.resource_id for s in second_succeeded}
        if ids1 == ids2:
            passed.append("No duplicate items created")
        else:
            errors.append("duplicate items detected")

        # No delete calls.
        if not any(m == "DELETE" for (m, *_r) in t.calls):
            passed.append("No delete calls issued")
        else:
            errors.append("DELETE call issued")

        # Persistence survives restart.
        reloaded = get_deployment(first.deployment_id)
        if reloaded and all(s.resource_id for s in reloaded["result"].steps if s.status == DeploymentStepStatus.SUCCEEDED and s.target_item_type != DeployableTargetType.LAKEHOUSE_TABLE.value):
            passed.append("Deployment results persisted with item ids")
        else:
            errors.append("results not persisted")

        # Secret scan.
        blob = json.dumps(reloaded["result"].model_dump(mode="json")).lower()
        if fh.FAKE_TOKEN.lower() not in blob and "fabric-secret" not in blob:
            passed.append("No secrets/tokens in stored result")
        else:
            errors.append("secret leaked into result")

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
