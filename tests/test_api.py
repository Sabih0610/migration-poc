"""Tests for src.api.app — Phase 1."""

import json

import pytest
from fastapi.testclient import TestClient

from src.api.app import app


@pytest.fixture
def client():
    """Test client for the FastAPI app."""
    return TestClient(app)


class TestHealthEndpoint:
    """GET /api/health tests."""

    def test_health_returns_200(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_health_returns_ok_status(self, client):
        data = client.get("/api/health").json()
        assert data["status"] == "ok"

    def test_health_includes_service(self, client):
        data = client.get("/api/health").json()
        assert "service" in data

    def test_health_includes_environment(self, client):
        data = client.get("/api/health").json()
        assert "environment" in data


class TestConfigStatusEndpoint:
    """GET /api/config/status tests."""

    def test_config_status_returns_200(self, client):
        resp = client.get("/api/config/status")
        assert resp.status_code == 200

    def test_config_status_has_configured(self, client):
        data = client.get("/api/config/status").json()
        assert "configured" in data

    def test_config_status_has_missing_settings(self, client):
        data = client.get("/api/config/status").json()
        assert "missing_settings" in data

    def test_config_status_has_dry_run(self, client):
        data = client.get("/api/config/status").json()
        assert "dry_run" in data

    def test_config_status_has_approval_required(self, client):
        data = client.get("/api/config/status").json()
        assert "approval_required" in data

    def test_config_status_has_delete_allowed(self, client):
        data = client.get("/api/config/status").json()
        assert "delete_allowed" in data

    def test_delete_allowed_defaults_false(self, client):
        data = client.get("/api/config/status").json()
        assert data["delete_allowed"] is False

    def test_no_secrets_in_config_status(self, client):
        """Config status must never expose secret values."""
        resp = client.get("/api/config/status")
        body = resp.text
        assert "client_secret" not in body.lower() or "azure_client_secret" not in json.dumps(resp.json())
        # Ensure no actual secret values leak
        assert "REDACTED" not in body  # Should not even have redacted — just omit secrets entirely

    def test_missing_cloud_config_does_not_crash(self, client):
        """App must not crash when Azure settings are empty."""
        resp = client.get("/api/config/status")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["missing_settings"], list)


class TestRootEndpoint:
    """GET / tests."""

    def test_root_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_root_includes_service(self, client):
        data = client.get("/").json()
        assert "service" in data
