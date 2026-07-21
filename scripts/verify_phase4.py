"""Phase 4 verification script — compatibility assessment engine.

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
from src.migration.assessment_store import get_assessment, save_assessment
from src.migration.discovery import ADFDiscoveryService
from src.models.schemas import AssessmentStatus

EXPECTED_OVERALL = AssessmentStatus.REQUIRES_CHANGE
EXPECTED_ASSET_COUNT = 14


def main() -> int:
    fixtures = PROJECT_ROOT / "fixtures"
    errors: list[str] = []
    passed: list[str] = []

    print("=" * 60)
    print("  Phase 4 Verification")
    print("=" * 60)
    print()

    # ── Discovery ────────────────────────────────────────────
    try:
        inv = load_mock_adf_inventory(fixtures)
        discovery = ADFDiscoveryService(inv).scan_inventory()
        passed.append("Discovery completed")
    except Exception as e:
        errors.append(f"Discovery failed: {e}")
        print(f"FATAL: {errors[-1]}")
        return 1

    # ── Assessment ───────────────────────────────────────────
    try:
        result = ADFCompatibilityAssessment(inv).assess_discovery(discovery)
        passed.append("Assessment completed")
    except Exception as e:
        errors.append(f"Assessment failed: {e}")
        print(f"FATAL: {errors[-1]}")
        return 1

    # ── All 14 assets assessed ───────────────────────────────
    if len(result.assessments) == EXPECTED_ASSET_COUNT:
        passed.append(f"All {EXPECTED_ASSET_COUNT} assets assessed")
    else:
        errors.append(
            f"Expected {EXPECTED_ASSET_COUNT} assessments, "
            f"got {len(result.assessments)}"
        )

    # ── Overall status ───────────────────────────────────────
    if result.overall_status == EXPECTED_OVERALL:
        passed.append(f"Overall status is {EXPECTED_OVERALL.value}")
    else:
        errors.append(
            f"Overall status {result.overall_status.value}, "
            f"expected {EXPECTED_OVERALL.value}"
        )

    # ── Required-change issues exist ─────────────────────────
    change_issues = [
        i
        for a in result.assessments
        for i in a.issues
        if i.status == AssessmentStatus.REQUIRES_CHANGE
    ]
    if change_issues:
        passed.append(f"Required-change issues exist: {len(change_issues)}")
    else:
        errors.append("No required-change issues found")

    # ── Trigger review exists ────────────────────────────────
    trigger_review = [
        a
        for a in result.assessments
        if a.asset_type == "trigger"
        and a.status == AssessmentStatus.NEEDS_REVIEW
    ]
    if trigger_review:
        passed.append("Trigger review exists")
    else:
        errors.append("No trigger needs-review found")

    # ── Zero unexpected blocked items ────────────────────────
    if result.summary.blocked_count == 0:
        passed.append("Zero blocked items")
    else:
        errors.append(f"Unexpected blocked items: {result.summary.blocked_count}")

    # ── SQLite save/load ─────────────────────────────────────
    try:
        init_database()
        run_id = save_assessment(result)
        loaded = get_assessment(run_id)
        if loaded and loaded["result"].overall_status == result.overall_status:
            passed.append(f"SQLite save/load works (id={run_id})")
        else:
            errors.append("SQLite save/load mismatch")
    except Exception as e:
        errors.append(f"SQLite save/load failed: {e}")

    # ── API works ────────────────────────────────────────────
    client = TestClient(app)
    scan = client.post("/api/discovery/scan")
    run = client.post("/api/assessment/run")
    latest = client.get("/api/assessment/latest")
    if (
        scan.status_code == 200
        and run.status_code == 200
        and latest.status_code == 200
    ):
        passed.append("API works (scan -> run -> latest)")
    else:
        errors.append(
            f"API failed: scan={scan.status_code} "
            f"run={run.status_code} latest={latest.status_code}"
        )

    # ── No secrets ───────────────────────────────────────────
    serialized = json.dumps(result.model_dump(mode="json"))
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
        passed.append("No secrets in assessment results")
    else:
        errors.append(f"Secrets found: {secrets}")

    # ── Report ───────────────────────────────────────────────
    print("  Overall status:  ", result.overall_status.value)
    print("  Assets assessed: ", len(result.assessments))
    print("  Status counts:   ", result.summary.status_counts)
    print("  Blocking issues: ", result.summary.blocking_issue_count)
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
