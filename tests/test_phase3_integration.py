"""Phase 3 integration tests."""

from fastapi.testclient import TestClient

from src.api.app import app

client = TestClient(app)


def test_get_assets_before_scan():
    response = client.get("/api/discovery/assets")
    assert response.status_code == 404


def test_scan_and_get():
    # Scan
    response = client.post("/api/discovery/scan")
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["discovery_id"] > 0
    assert response.json()["summary"]["artifact_count"] == 10
    assert response.json()["summary"]["component_count"] == 11

    # Get assets
    response = client.get("/api/discovery/assets")
    assert response.status_code == 200
    assert response.json()["count"] > 0

    # Get dependencies
    response = client.get("/api/discovery/dependencies")
    assert response.status_code == 200
    assert "dependencies" in response.json()
    assert "missing_dependencies" in response.json()

    # Get summary
    response = client.get("/api/discovery/summary")
    assert response.status_code == 200
    assert response.json()["pipeline_count"] == 1

    # A new client/process-facing request reconstructs discovery from SQLite;
    # no module-level discovery cache is required.
    restarted_client = TestClient(app)
    latest = restarted_client.get("/api/discovery/latest")
    assert latest.status_code == 200
    assert latest.json()["result"]["inventory"]["pipelines"][0]["name"] == (
        "pl_sales_processing_legacy"
    )
