"""Unit tests for the read-only Azure ADF connector (mocked SDK)."""

import pytest

from src.connectors.azure_adf_client import (
    CODE_AUTH,
    CODE_AUTHZ,
    CODE_BOUNDARY,
    CODE_CONFIG,
    CODE_MALFORMED,
    CODE_NOT_FOUND,
    CODE_TIMEOUT,
    AzureADFClient,
    AzureDiscoveryError,
)
from tests import azure_helpers as az


# ── Configuration & boundary ─────────────────────────────────────


def test_incomplete_config_raises():
    with pytest.raises(AzureDiscoveryError) as exc:
        AzureADFClient(
            tenant_id="t", client_id="c", client_secret="s",
            subscription_id="", resource_group="rg", data_factory_name="df",
        )
    assert exc.value.code == CODE_CONFIG


def test_verify_environment_ok():
    client = az.make_client()
    env = client.verify_environment()
    assert env["subscription_accessible"] is True
    assert env["resource_group"] == az.TEST_RG
    assert env["boundary_enforced"] is True


def test_verify_data_factory_ok():
    client = az.make_client()
    df = client.verify_data_factory()
    assert df["exists"] is True
    assert df["name"] == az.TEST_DF
    assert df["location"] == "northcentralus"


def test_provider_status_ok():
    client = az.make_client()
    assert client.provider_status("Microsoft.DataFactory")["registration_state"] == "Registered"
    assert client.provider_status("Microsoft.Storage")["registration_state"] == "Registered"


def test_provider_status_rejects_other_namespaces():
    client = az.make_client()
    with pytest.raises(AzureDiscoveryError) as exc:
        client.provider_status("Microsoft.Compute")
    assert exc.value.code == CODE_BOUNDARY


def test_boundary_violation_on_foreign_resource_id():
    # A dataset whose id points at a different subscription must be rejected.
    raw = az.fixture_raw_definitions()
    raw["datasets"][0]["id"] = (
        "/subscriptions/99999999-9999-9999-9999-999999999999/resourceGroups/"
        f"{az.TEST_RG}/providers/Microsoft.DataFactory/factories/{az.TEST_DF}/datasets/x"
    )
    client = az.make_client(raw=raw)
    with pytest.raises(AzureDiscoveryError) as exc:
        client.list_datasets()
    assert exc.value.code == CODE_BOUNDARY


# ── Listing + complete definitions ───────────────────────────────


def test_list_pipelines_preserves_full_definition():
    client = az.make_client()
    pipelines = client.list_pipelines()
    assert len(pipelines) == 1
    p = pipelines[0]
    assert p["name"] == "pl_sales_processing_legacy"
    # Nested IfCondition + activities preserved losslessly.
    activities = p["properties"]["activities"]
    assert any(a["type"] == "IfCondition" for a in activities)
    assert "parameters" in p["properties"]


def test_discover_raw_returns_all_asset_types():
    client = az.make_client()
    raw = client.discover_raw()
    assert set(raw) == {
        "pipelines", "data_flows", "datasets", "linked_services", "triggers"
    }
    assert len(raw["datasets"]) == 6
    assert len(raw["data_flows"]) == 1
    assert len(raw["triggers"]) == 1


# ── Error mapping (sanitized) ────────────────────────────────────


def _sdk_error(name, status=None):
    exc = type(name, (Exception,), {})("raw sdk detail that must not leak")
    if status is not None:
        exc.status_code = status
    return exc


def _client_where_list_raises(exc):
    client = az.make_client()

    def boom(*a, **k):
        raise exc

    client._datafactory().pipelines.list_by_factory = boom
    return client


@pytest.mark.parametrize(
    "name,status,expected",
    [
        ("ClientAuthenticationError", None, CODE_AUTH),
        ("HttpResponseError", 401, CODE_AUTH),
        ("HttpResponseError", 403, CODE_AUTHZ),
        ("ResourceNotFoundError", 404, CODE_NOT_FOUND),
        ("ServiceRequestError", None, CODE_TIMEOUT),
    ],
)
def test_error_classification(name, status, expected):
    client = _client_where_list_raises(_sdk_error(name, status))
    with pytest.raises(AzureDiscoveryError) as raised:
        client.list_pipelines()
    assert raised.value.code == expected
    # Raw SDK detail is never surfaced.
    assert "raw sdk detail" not in raised.value.message


def test_timeout_error_classification():
    client = _client_where_list_raises(TimeoutError("slow network"))
    with pytest.raises(AzureDiscoveryError) as raised:
        client.list_pipelines()
    assert raised.value.code == CODE_TIMEOUT


def test_malformed_definition_rejected():
    # A definition dict missing 'name', and a non-serializable object.
    with pytest.raises(AzureDiscoveryError) as e1:
        AzureADFClient._to_definition({"properties": {}})
    assert e1.value.code == CODE_MALFORMED

    class Weird:
        pass

    with pytest.raises(AzureDiscoveryError) as e2:
        AzureADFClient._to_definition(Weird())
    assert e2.value.code == CODE_MALFORMED


# ── No write / destructive surface ───────────────────────────────


def test_client_exposes_no_write_methods():
    client = az.make_client()
    for attr in dir(client):
        low = attr.lower()
        for banned in ("create", "update", "delete", "publish", "execute",
                       "register", "remove", "drop", "write"):
            assert banned not in low, f"unexpected method: {attr}"


def test_secret_not_in_describe_or_errors():
    from src.connectors.azure_adf_source import AzureADFSource

    client = az.make_client()
    described = AzureADFSource(client).describe()
    assert "SECRET-should-never-leak" not in str(described)
