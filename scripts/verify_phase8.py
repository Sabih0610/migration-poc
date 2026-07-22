"""Verify optional runtime metrics remain separate from structural status."""

import sys
from pathlib import Path
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_helper import TempDatabase
from src.api.app import app
from src.database import RuntimeValidationRunRecord, StructuralValidationRunRecord, get_session_factory


def main() -> int:
    print("Phase 8 Optional Runtime Verification")
    with TempDatabase(prefix="verify_phase8_runtime_"):
        client = TestClient(app)
        client.post("/api/discovery/scan")
        client.post("/api/assessment/run")
        plan_id = client.post("/api/plans/generate").json()["plan_id"]
        approval_id = client.post(f"/api/plans/{plan_id}/request-approval", json={"user": "alice"}).json()["approval_id"]
        client.post(f"/api/approvals/{approval_id}/approve", json={"user": "bob"})
        deployment_id = client.post("/api/deployments/start", json={"plan_id": plan_id, "approval_id": approval_id, "mode": "MOCK"}).json()["deployment_id"]
        response = client.post("/api/runtime-validations/run", json={"deployment_id": deployment_id})
        session = get_session_factory()()
        try:
            separated = session.query(RuntimeValidationRunRecord).count() == 1 and session.query(StructuralValidationRunRecord).count() == 0
        finally:
            session.close()
        passed = response.status_code == 200 and separated
        print("  [OK] Runtime metrics persisted separately" if passed else f"  [FAIL] {response.text}")
        print(f"  RESULT: {'PASS' if passed else 'FAIL'}")
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
