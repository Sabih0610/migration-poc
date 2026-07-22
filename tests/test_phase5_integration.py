"""Phase 5 integration tests — migration plan API."""

import json

from fastapi.testclient import TestClient

import src.api.routes as discovery_routes
from src.api.app import app
from src.database import init_database

# The module-level TestClient does not trigger the app lifespan, so
# create the tables (incl. migration_plans) explicitly.
init_database()

client = TestClient(app)


def _reset_discovery():
    discovery_routes._latest_result = None
    discovery_routes._latest_inventory = None
    discovery_routes._latest_graph = None


def test_generate_requires_discovery():
    _reset_discovery()
    response = client.post("/api/plans/generate")
    assert response.status_code == 409


def test_generate_requires_assessment(monkeypatch):
    # Discovery present, but no assessment available -> 409.
    assert client.post("/api/discovery/scan").status_code == 200
    import src.api.plan_routes as plan_routes

    monkeypatch.setattr(plan_routes, "get_latest_assessment", lambda: None)
    response = client.post("/api/plans/generate")
    assert response.status_code == 409
    assert "assessment" in response.json()["detail"].lower()


def test_full_flow_scan_assess_plan():
    assert client.post("/api/discovery/scan").status_code == 200
    assert client.post("/api/assessment/run").status_code == 200

    generate = client.post("/api/plans/generate")
    assert generate.status_code == 200
    body = generate.json()
    assert body["status"] == "completed"
    assert body["executable"] is True
    assert body["overall_risk"] == "MEDIUM"
    assert body["summary"]["total_source_assets"] == 14
    assert body["summary"]["action_count"] == 11
    assert body["summary"]["validation_rule_count"] == 10
    assert body["generated_artifact_count"] == 8
    assert body["package_id"].startswith("package-")
    assert body["package_manifest_path"].startswith("manifests/")
    plan_id = body["plan_id"]

    # Latest returns the same plan.
    latest = client.get("/api/plans/latest")
    assert latest.status_code == 200
    assert latest.json()["plan_id"] == plan_id

    # Fetch by id.
    by_id = client.get(f"/api/plans/{plan_id}")
    assert by_id.status_code == 200
    assert by_id.json()["plan"]["executable"] is True
    assert len(by_id.json()["plan"]["actions"]) == 11
    assert len(by_id.json()["plan"]["generated_package"]["artifacts"]) == 8

    package = client.get(f"/api/plans/{plan_id}/package")
    assert package.status_code == 200
    assert len(package.json()["package"]["artifacts"]) == 8


def test_version_increments_via_api():
    client.post("/api/discovery/scan")
    client.post("/api/assessment/run")
    first = client.post("/api/plans/generate").json()
    second = client.post("/api/plans/generate").json()
    # Same assessment id -> version increments.
    assert second["version"] == first["version"] + 1


def test_unknown_plan_id_returns_404():
    assert client.get("/api/plans/999999").status_code == 404


def test_plan_api_has_no_secrets():
    client.post("/api/discovery/scan")
    client.post("/api/assessment/run")
    client.post("/api/plans/generate")
    latest = client.get("/api/plans/latest")
    serialized = json.dumps(latest.json())
    for token in (
        "password",
        "client_secret",
        "accountKey",
        "connectionString",
        "accessToken",
        "servicePrincipalKey",
    ):
        assert token not in serialized
