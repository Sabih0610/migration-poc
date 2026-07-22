"""Phase 9 API integration tests — read-only Azure endpoints (mocked)."""

import json

from fastapi.testclient import TestClient

import src.api.azure_routes as azure_routes
from src.api.app import app
from src.connectors.azure_adf_source import AzureADFSource
from tests import azure_helpers as az

client = TestClient(app)


def test_azure_status_does_not_leak_secrets_and_is_offline():
    resp = client.get("/api/azure/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "enabled" in body and "ready" in body
    # No secret material in the status payload.
    assert "SECRET" not in json.dumps(body)
    assert "client_secret" not in json.dumps(body)


def test_verify_disabled_returns_409_without_azure_call():
    # Default settings have discovery disabled -> 409, no Azure contact.
    resp = client.post("/api/azure/verify")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "AZURE_DISCOVERY_DISABLED"


def test_verify_success_with_mocked_client(monkeypatch):
    # Bypass the enable gate by injecting a fake-backed client builder.
    monkeypatch.setattr(
        azure_routes, "build_azure_adf_client_from_settings",
        lambda settings: az.make_client(),
    )
    resp = client.post("/api/azure/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["environment"]["subscription_accessible"] is True
    assert body["data_factory"]["name"] == az.TEST_DF
    assert body["providers"]["Microsoft.DataFactory"]["registration_state"] == "Registered"
    assert body["providers"]["Microsoft.Storage"]["registration_state"] == "Registered"
    # No secret leaks in the verification response.
    assert "SECRET" not in json.dumps(body)


def test_verify_maps_not_found(monkeypatch):
    def _factory(settings):
        c = az.make_client()

        def boom(*a, **k):
            raise type("ResourceNotFoundError", (Exception,), {})("x")

        c._resources().resource_groups.get = boom
        return c

    monkeypatch.setattr(
        azure_routes, "build_azure_adf_client_from_settings", _factory
    )
    resp = client.post("/api/azure/verify")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "AZURE_NOT_FOUND"
