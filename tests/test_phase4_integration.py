"""Phase 4 integration tests — assessment API."""

import json

from fastapi.testclient import TestClient

import src.api.routes as discovery_routes
from src.api.app import app
from src.database import init_database

# The module-level TestClient below does not trigger the app lifespan,
# so create the tables (incl. assessment_runs) explicitly.
init_database()

client = TestClient(app)


def _reset_discovery():
    """Clear in-memory discovery state so 'no scan' can be tested."""
    discovery_routes._latest_result = None
    discovery_routes._latest_inventory = None
    discovery_routes._latest_graph = None


def test_run_requires_discovery():
    _reset_discovery()
    response = client.post("/api/assessment/run")
    assert response.status_code == 409


def test_latest_before_any_run():
    # After a fresh discovery but before an assessment run, /latest may
    # return an earlier run or 404; either way it must not 500.
    assert client.get("/api/assessment/latest").status_code in (200, 404)


def test_scan_run_and_fetch():
    # Discovery first.
    assert client.post("/api/discovery/scan").status_code == 200

    # Run assessment.
    run = client.post("/api/assessment/run")
    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "completed"
    assert body["overall_status"] == "REQUIRES_CHANGE"
    assert body["summary"]["total_assets"] == 14
    assessment_id = body["assessment_id"]

    # Latest returns the same run.
    latest = client.get("/api/assessment/latest")
    assert latest.status_code == 200
    assert latest.json()["assessment_id"] == assessment_id
    assert latest.json()["result"]["overall_status"] == "REQUIRES_CHANGE"

    # Fetch by id.
    by_id = client.get(f"/api/assessment/{assessment_id}")
    assert by_id.status_code == 200
    assert by_id.json()["assessment_id"] == assessment_id


def test_unknown_id_returns_404():
    assert client.get("/api/assessment/999999").status_code == 404


def test_api_response_has_no_secrets():
    client.post("/api/discovery/scan")
    client.post("/api/assessment/run")
    latest = client.get("/api/assessment/latest")
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
