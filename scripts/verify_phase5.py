"""Phase 5 verification script — migration planner.

Exit code 0 = PASS, 1 = FAIL.
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from src.api.app import app
from src.database import init_database
from src.fixtures_loader import load_mock_adf_inventory
from src.migration.assessment import ADFCompatibilityAssessment
from src.migration.discovery import ADFDiscoveryService
from src.migration.plan_store import get_plan, save_plan
from src.migration.planner import MigrationPlanner
from src.models.schemas import MigrationActionType, TargetItemType

CANONICAL_ORDER = [
    MigrationActionType.VERIFY_WORKSPACE,
    MigrationActionType.CREATE_CONNECTION,
    MigrationActionType.CREATE_LAKEHOUSE,
    MigrationActionType.CREATE_TABLE,
    MigrationActionType.CREATE_DATAFLOW,
    MigrationActionType.CREATE_PIPELINE,
    MigrationActionType.CONFIGURE_SCHEDULE,
    MigrationActionType.RUN_TARGET,
    MigrationActionType.VALIDATE,
]


def _order_is_valid(actions) -> bool:
    """Deployment actions must appear in canonical phase order."""
    rank = {t: i for i, t in enumerate(CANONICAL_ORDER)}
    ranks = [rank[a.action_type] for a in actions]
    return ranks == sorted(ranks) and [a.order for a in actions] == list(
        range(1, len(actions) + 1)
    )


def main() -> int:
    fixtures = PROJECT_ROOT / "fixtures"
    errors: list[str] = []
    passed: list[str] = []

    print("=" * 60)
    print("  Phase 5 Verification")
    print("=" * 60)
    print()

    # ── Discovery + assessment + plan ────────────────────────
    try:
        inv = load_mock_adf_inventory(fixtures)
        discovery = ADFDiscoveryService(inv).scan_inventory()
        passed.append("Discovery completed")
    except Exception as e:
        errors.append(f"Discovery failed: {e}")
        print(f"FATAL: {errors[-1]}")
        return 1

    try:
        assessment = ADFCompatibilityAssessment(inv).assess_discovery(discovery)
        passed.append("Assessment completed")
    except Exception as e:
        errors.append(f"Assessment failed: {e}")
        return 1

    try:
        plan = MigrationPlanner(inv).generate_plan(discovery, assessment)
        passed.append("Plan generated")
    except Exception as e:
        errors.append(f"Plan generation failed: {e}")
        return 1

    # ── Every asset mapped or explained ──────────────────────
    unexplained = [
        m.source_asset
        for m in plan.mappings
        if not m.mapped and not m.explanation
    ]
    if len(plan.mappings) == 14 and not unexplained:
        passed.append(f"All {len(plan.mappings)} assets mapped or explained")
    else:
        errors.append(f"Unmapped/unexplained assets: {unexplained}")

    # ── Fabric targets exist ─────────────────────────────────
    targets = {m.target_item_type for m in plan.mappings if m.mapped}
    if TargetItemType.DATA_PIPELINE in targets:
        passed.append("Target Fabric pipeline exists")
    else:
        errors.append("No Fabric pipeline target")

    if TargetItemType.DATAFLOW_GEN2 in targets:
        passed.append("Target Dataflow Gen2 exists")
    else:
        errors.append("No Dataflow Gen2 target")

    action_types = {a.action_type for a in plan.actions}
    if (
        MigrationActionType.CREATE_LAKEHOUSE in action_types
        and TargetItemType.LAKEHOUSE_TABLE in targets
    ):
        passed.append("Target Lakehouse items exist")
    else:
        errors.append("No Lakehouse / table targets")

    # ── Deployment order valid ───────────────────────────────
    if _order_is_valid(plan.actions):
        passed.append(f"Deployment order valid ({len(plan.actions)} actions)")
    else:
        errors.append("Deployment order invalid")

    # ── Validation rules exist ───────────────────────────────
    if len(plan.validation_rules) >= 9:
        passed.append(f"Validation rules exist: {len(plan.validation_rules)}")
    else:
        errors.append(f"Too few validation rules: {len(plan.validation_rules)}")

    # ── Plan executable ──────────────────────────────────────
    if plan.executable:
        passed.append("Plan is executable")
    else:
        errors.append("Plan is not executable")

    # ── SQLite save/load ─────────────────────────────────────
    try:
        init_database()
        record = save_plan(plan, assessment_id=1)
        loaded = get_plan(record["id"])
        if loaded and loaded["plan"].overall_risk == plan.overall_risk:
            passed.append(
                f"SQLite save/load works (id={record['id']}, v{record['version']})"
            )
        else:
            errors.append("SQLite save/load mismatch")
    except Exception as e:
        errors.append(f"SQLite save/load failed: {e}")

    # ── API flow ─────────────────────────────────────────────
    client = TestClient(app)
    scan = client.post("/api/discovery/scan")
    assess = client.post("/api/assessment/run")
    generate = client.post("/api/plans/generate")
    latest = client.get("/api/plans/latest")
    if all(
        r.status_code == 200 for r in (scan, assess, generate, latest)
    ):
        passed.append("API works (scan -> assessment -> plan)")
    else:
        errors.append(
            f"API failed: scan={scan.status_code} assess={assess.status_code} "
            f"generate={generate.status_code} latest={latest.status_code}"
        )

    # ── No secrets ───────────────────────────────────────────
    serialized = json.dumps(plan.model_dump(mode="json"))
    secrets = [
        s
        for s in (
            "password",
            "client_secret",
            "accountKey",
            "connectionString",
            "accessToken",
            "servicePrincipalKey",
        )
        if s in serialized
    ]
    if not secrets:
        passed.append("No secrets in plan")
    else:
        errors.append(f"Secrets found: {secrets}")

    # ── Report ───────────────────────────────────────────────
    print("  Executable:      ", plan.executable)
    print("  Overall risk:    ", plan.overall_risk.value)
    print("  Assets mapped:   ", plan.summary.mapped_count, "/", plan.summary.total_source_assets)
    print("  Actions:         ", plan.summary.action_count)
    print("  Manual actions:  ", plan.summary.manual_action_count)
    print("  Validation rules:", plan.summary.validation_rule_count)
    print()
    print("-" * 60)
    print(f"  PASSED: {len(passed)}")
    for p in passed:
        print(f"    [OK] {p}")
    print()

    if errors:
        print(f"  FAILED: {len(errors)}")
        for e in errors:
            print(f"    [FAIL] {e}")
        print()
        print("  RESULT: FAIL")
        return 1

    print("  FAILED: 0")
    print()
    print("  RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
