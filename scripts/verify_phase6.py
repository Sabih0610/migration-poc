"""Phase 6 verification script — approval & deployment guard.

Exit code 0 = PASS, 1 = FAIL.
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database as db_module
from src.api.app import app
from src.approvals import approval_service as svc
from src.database import Base
from src.fixtures_loader import load_mock_adf_inventory
from src.migration.assessment import ADFCompatibilityAssessment
from src.migration.discovery import ADFDiscoveryService
from src.migration.plan_store import save_plan
from src.migration.planner import MigrationPlanner
from src.models.schemas import ApprovalStatus

ASSESSMENT_ID = 700001


def _use_temp_database() -> str:
    """Point the database module at a throwaway SQLite file.

    Keeps verification runs isolated from migration_poc.db and makes
    repeated runs produce identical results. Returns the temp dir path.
    """
    tmp_dir = tempfile.mkdtemp(prefix="verify_phase6_")
    url = f"sqlite:///{(Path(tmp_dir) / 'verify.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    db_module._engine = engine
    db_module._SessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    Base.metadata.create_all(bind=engine)
    return tmp_dir


def main() -> int:
    print("=" * 60)
    print("  Phase 6 Verification")
    print("=" * 60)
    print()

    tmp_dir = _use_temp_database()
    try:
        return _run()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _run() -> int:
    fixtures = PROJECT_ROOT / "fixtures"
    errors: list[str] = []
    passed: list[str] = []

    # ── Discovery + assessment + plan ────────────────────────
    try:
        inv = load_mock_adf_inventory(fixtures)
        discovery = ADFDiscoveryService(inv).scan_inventory()
        assessment = ADFCompatibilityAssessment(inv).assess_discovery(discovery)
        plan = MigrationPlanner(inv).generate_plan(discovery, assessment)
        v1 = save_plan(plan, assessment_id=ASSESSMENT_ID)
        passed.append("Discovery + assessment + plan generation")
    except Exception as e:
        errors.append(f"Setup failed: {e}")
        print(f"FATAL: {errors[-1]}")
        return 1

    # ── Request approval ─────────────────────────────────────
    approval = svc.request_approval(v1["id"], "alice", "please review")
    if approval.status == ApprovalStatus.PENDING:
        passed.append("Approval requested (PENDING)")
    else:
        errors.append("Approval not PENDING after request")

    # ── Deployment blocked before approval ───────────────────
    if not svc.can_deploy(v1["id"], approval.approval_id):
        passed.append("Deployment blocked before approval")
    else:
        errors.append("Deployment allowed while pending")

    # ── Approve ──────────────────────────────────────────────
    svc.approve(approval.approval_id, "bob", "looks good")
    if svc.can_deploy(v1["id"], approval.approval_id):
        passed.append("Deployment allowed after approval")
    else:
        errors.append("Deployment blocked after approval")

    # ── New plan version invalidates old approval ────────────
    v2 = save_plan(plan, assessment_id=ASSESSMENT_ID)
    svc.invalidate_stale_approvals(v2["id"])
    old = svc.get_status(approval.approval_id)
    if old.status == ApprovalStatus.INVALIDATED and not svc.can_deploy(
        v1["id"], approval.approval_id
    ):
        passed.append("Old approval invalidated by new plan version")
    else:
        errors.append("Old approval not invalidated after new version")

    # ── Reject another request ───────────────────────────────
    approval2 = svc.request_approval(v2["id"], "alice", "second review")
    svc.reject(approval2.approval_id, "bob", "not yet")
    if not svc.can_deploy(v2["id"], approval2.approval_id):
        passed.append("Rejected approval blocks deployment")
    else:
        errors.append("Rejected approval still deployable")

    # ── API flow ─────────────────────────────────────────────
    client = TestClient(app)
    client.post("/api/discovery/scan")
    client.post("/api/assessment/run")
    plan_id = client.post("/api/plans/generate").json()["plan_id"]
    req = client.post(
        f"/api/plans/{plan_id}/request-approval", json={"user": "alice"}
    )
    appr_id = req.json().get("approval_id")
    approve = client.post(f"/api/approvals/{appr_id}/approve", json={"user": "bob"})
    status = client.get(f"/api/plans/{plan_id}/approval-status")
    if (
        req.status_code == 200
        and approve.status_code == 200
        and status.status_code == 200
        and status.json()["can_deploy"] is True
    ):
        passed.append("API flow works (request -> approve -> can deploy)")
    else:
        errors.append("API flow failed")

    # ── Approval page ────────────────────────────────────────
    page = client.get("/approval")
    if page.status_code == 200 and "Approval" in page.text:
        passed.append("Approval page loads")
    else:
        errors.append("Approval page failed to load")

    # ── Secrets scan ─────────────────────────────────────────
    blob = json.dumps(status.json())
    secrets = [
        s
        for s in (
            "password", "client_secret", "accountKey",
            "connectionString", "accessToken", "servicePrincipalKey",
        )
        if s in blob
    ]
    if not secrets:
        passed.append("No secrets in approval output")
    else:
        errors.append(f"Secrets found: {secrets}")

    # ── Report ───────────────────────────────────────────────
    summary = svc.approval_store.get_summary()
    print("  Approval summary:", summary.model_dump())
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
