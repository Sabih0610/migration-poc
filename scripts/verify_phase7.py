"""Phase 7 verification script — Mock Fabric Deployment Engine.

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
from src.database import Base
from src.connectors.mock_fabric_client import MockFabricClient
from src.migration.deployment import DeploymentService
from src.models.schemas import DeploymentMode, DeploymentStatus, DeploymentStepStatus

from scripts.verify_helper import TempDatabase

def main() -> int:
    print("=" * 60)
    print("  Phase 7 Verification")
    print("=" * 60)
    print()

    with TempDatabase():
        return _run()

def _run() -> int:
    client = TestClient(app)
    errors: list[str] = []
    passed: list[str] = []

    # 1. Setup Data (Discovery -> Assessment -> Plan -> Approve)
    try:
        client.post("/api/discovery/scan")
        client.post("/api/assessment/run")
        plan_id = client.post("/api/plans/generate").json()["plan_id"]
        req = client.post(f"/api/plans/{plan_id}/request-approval", json={"user": "alice"})
        appr_id = req.json()["approval_id"]
        client.post(f"/api/approvals/{appr_id}/approve", json={"user": "bob"})
        passed.append("Setup complete (Discovery -> Approve)")
    except Exception as e:
        print(f"FATAL SETUP ERROR: {e}")
        return 1

    # 2. Dry Run
    resp = client.post("/api/deployments/start", json={"plan_id": plan_id, "approval_id": appr_id, "mode": "DRY_RUN"})
    if resp.status_code == 200:
        data = resp.json()
        if data["summary"]["resources_created"] == 0 and data["status"] == "SUCCEEDED":
            passed.append("DRY_RUN creates zero resources")
        else:
            errors.append("DRY_RUN created resources or failed")
    else:
        errors.append("DRY_RUN API failed")

    # 3. Mock Deployment
    resp = client.post("/api/deployments/start", json={"plan_id": plan_id, "approval_id": appr_id, "mode": "MOCK"})
    if resp.status_code == 200:
        data = resp.json()
        if data["summary"]["resources_created"] > 0 and data["status"] == "SUCCEEDED":
            passed.append("MOCK deployment created expected resources")
        else:
            errors.append("MOCK deployment failed to create resources")
            
        validate_step = next((s for s in data["steps"] if s["action_type"] == "validate"), None)
        if validate_step and validate_step["status"] == "SKIPPED":
            passed.append("Validate step deferred")
        else:
            errors.append("Validate step was not deferred")
            
        # Check order
        steps = data["steps"]
        orders = [s["order"] for s in steps]
        if orders == sorted(orders):
            passed.append("Deployment steps are ordered")
        else:
            errors.append("Deployment steps out of order")
    else:
        errors.append("MOCK API failed")

    # 4. Repeat Mock Deployment (Idempotent)
    connector = MockFabricClient()
    svc = DeploymentService(connector=connector)
    
    # First deploy
    res1 = svc.deploy(plan_id, appr_id, DeploymentMode.MOCK)
    count1 = connector.resource_count()
    
    # Second deploy (should be 0 new resources)
    res2 = svc.deploy(plan_id, appr_id, DeploymentMode.MOCK)
    count2 = connector.resource_count()
    
    if count1 > 0 and count1 == count2:
        passed.append("Repeat run is idempotent (0 new resources)")
    else:
        errors.append("Repeat run created duplicates")

    # 5. Inject failure
    fail_connector = MockFabricClient(fail_on_action="create_table")
    fail_svc = DeploymentService(connector=fail_connector)
    fail_res = fail_svc.deploy(plan_id, appr_id, DeploymentMode.MOCK)
    
    if fail_res.status == DeploymentStatus.PARTIAL or fail_res.status == DeploymentStatus.FAILED:
        failed_step = next((s for s in fail_res.steps if s.status == DeploymentStepStatus.FAILED), None)
        if failed_step and failed_step.action_type == "create_table":
            passed.append("Injected failure recorded on create_table")
            
            # Ensure subsequent steps are skipped
            later_steps = [s for s in fail_res.steps if s.order > failed_step.order]
            if all(s.status == DeploymentStepStatus.SKIPPED for s in later_steps):
                passed.append("Later steps stopped after failure")
            else:
                errors.append("Later steps were not skipped after failure")
        else:
            errors.append("Failed step not recorded correctly")
    else:
        errors.append("Injected failure did not cause deployment failure")

    # 6. Test deployment page
    page = client.get("/deployment")
    if page.status_code == 200 and "Deployment" in page.text:
        passed.append("Deployment page loads")
    else:
        errors.append("Deployment page failed to load")

    # 7. Secret scan
    blob = json.dumps(resp.json())
    secrets = [
        s for s in ("password", "client_secret", "accountKey", "connectionString")
        if s in blob
    ]
    if not secrets:
        passed.append("No secrets in deployment output")
    else:
        errors.append(f"Secrets found: {secrets}")

    # ── Report ───────────────────────────────────────────────
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
