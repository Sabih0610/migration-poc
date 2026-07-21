"""Integration tests for Phase 7 (Deployment)."""

from fastapi.testclient import TestClient
from src.api.app import app
from src.connectors.mock_fabric_client import MockFabricClient

client = TestClient(app)

def test_phase7_api_flow():
    # 1. Run discovery
    resp = client.post("/api/discovery/scan")
    assert resp.status_code == 200

    # 2. Run assessment
    resp = client.post("/api/assessment/run")
    assert resp.status_code == 200

    # 3. Generate plan
    resp = client.post("/api/plans/generate")
    assert resp.status_code == 200
    plan_id = resp.json()["plan_id"]

    # 4. Request approval
    resp = client.post(f"/api/plans/{plan_id}/request-approval", json={"user": "alice"})
    assert resp.status_code == 200
    approval_id = resp.json()["approval_id"]

    # 5. Approve plan
    resp = client.post(f"/api/approvals/{approval_id}/approve", json={"user": "bob"})
    assert resp.status_code == 200

    # 6. Start DRY_RUN deployment
    payload = {
        "plan_id": plan_id,
        "approval_id": approval_id,
        "mode": "DRY_RUN"
    }
    resp = client.post("/api/deployments/start", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "SUCCEEDED"
    assert data["summary"]["resources_created"] == 0
    assert data["mode"] == "DRY_RUN"
    
    # 7. Start MOCK deployment
    payload["mode"] = "MOCK"
    resp = client.post("/api/deployments/start", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "SUCCEEDED"
    assert data["summary"]["resources_created"] > 0
    assert data["mode"] == "MOCK"
    
    # Check that VALIDATE step is deferred/skipped
    validate_step = next((s for s in data["steps"] if s["action_type"] == "validate"), None)
    assert validate_step is not None
    assert validate_step["status"] == "SKIPPED"
    
    # 8. Check endpoints
    resp = client.get("/api/deployments/latest")
    assert resp.status_code == 200
    
    deployment_id = resp.json()["deployment_id"]
    resp = client.get(f"/api/deployments/{deployment_id}")
    assert resp.status_code == 200
    
    # 9. UI
    resp = client.get("/deployment")
    assert resp.status_code == 200

def test_phase7_blocked_by_guard():
    # Attempt to deploy with an unapproved approval_id
    payload = {
        "plan_id": 999,
        "approval_id": 999,
        "mode": "DRY_RUN"
    }
    resp = client.post("/api/deployments/start", json=payload)
    assert resp.status_code == 409

def test_phase7_invalid_mode():
    payload = {
        "plan_id": 1,
        "approval_id": 1,
        "mode": "INVALID"
    }
    resp = client.post("/api/deployments/start", json=payload)
    assert resp.status_code == 400
