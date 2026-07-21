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
