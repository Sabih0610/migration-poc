"""Unit tests for the read/write Fabric connector (mocked HTTP)."""

import pytest

from src.connectors.fabric_client import (
    CODE_AUTH,
    CODE_AUTHZ,
    CODE_BOUNDARY,
    CODE_CONFIG,
    CODE_CONFLICT,
    CODE_DISABLED,
    CODE_NOT_FOUND,
    CODE_SCHEMA,
    CODE_THROTTLED,
    CODE_TIMEOUT,
    FabricClient,
    FabricError,
    build_fabric_client_from_settings,
)
from src.models.schemas import DeployableTargetType, GeneratedArtifact
from tests import fabric_helpers as fh


# ── Config / auth ────────────────────────────────────────────────


def test_incomplete_config_raises():
    with pytest.raises(FabricError) as exc:
        FabricClient(tenant_id="t", client_id="c", client_secret="s", workspace_id="")
    assert exc.value.code == CODE_CONFIG


def test_verify_authentication_ok():
    assert fh.make_client().verify_authentication() == {"authenticated": True}


def test_auth_failure_maps_code():
    def boom():
        raise RuntimeError("bad creds with raw detail")

    client = fh.make_client(token_provider=boom)
    with pytest.raises(FabricError) as exc:
        client.verify_authentication()
    assert exc.value.code == CODE_AUTH
    assert "raw detail" not in exc.value.message


# ── Read-only verification ───────────────────────────────────────


def test_verify_workspace_and_capacity_and_roles():
    client = fh.make_client()
    ws = client.verify_workspace()
    assert ws["accessible"] and ws["workspace_id"] == fh.WS
    cap = client.verify_capacity()
    assert cap["state"] == "Active" and cap["matches_config"] is True
    perms = client.verify_permissions()
    assert perms["has_role"] and "Admin" in perms["principal_roles"]


def test_verify_environment_aggregates():
    env = fh.make_client().verify_environment()
    assert set(env) >= {"authentication", "workspace", "capacity", "permissions", "item_count"}


def test_workspace_id_mismatch_is_boundary():
    t = fh.FakeFabricTransport()
    t.workspace = {"id": "some-other-ws", "displayName": "x"}
    with pytest.raises(FabricError) as exc:
        fh.make_client(transport=t).verify_workspace()
    assert exc.value.code == CODE_BOUNDARY


def test_foreign_item_rejected_on_boundary():
    t = fh.FakeFabricTransport()
    t.items = [{"id": "i1", "type": "Lakehouse", "displayName": "x", "workspaceId": "other"}]
    with pytest.raises(FabricError) as exc:
        fh.make_client(transport=t).list_items()
    assert exc.value.code == CODE_BOUNDARY


def test_not_found_and_authz_mapping():
    t = fh.FakeFabricTransport()
    t.force = (f"/workspaces/{fh.WS}", 404)
    with pytest.raises(FabricError) as exc:
        fh.make_client(transport=t).verify_workspace()
    assert exc.value.code == CODE_NOT_FOUND

    t2 = fh.FakeFabricTransport()
    t2.force = ("/roleAssignments", 403)
    with pytest.raises(FabricError) as exc2:
        fh.make_client(transport=t2).verify_permissions()
    assert exc2.value.code == CODE_AUTHZ


# ── Create / reuse ───────────────────────────────────────────────


def test_create_then_reuse_is_idempotent():
    package = fh.full_package()
    lakehouse = fh.artifact_of(package, DeployableTargetType.LAKEHOUSE)
    client = fh.make_client()

    first = client.deploy_artifact(lakehouse)
    assert first.status == "created" and first.item_id and first.reused is False
    assert first.content_digest == lakehouse.content_digest

    second = client.deploy_artifact(lakehouse)
    assert second.status == "reused" and second.reused is True
    assert second.item_id == first.item_id


def test_deploy_each_supported_type_creates():
    """Exercise every DeployableTargetType through the corrected client.

    Connection, LakehouseTable, and FabricSchedule are NOT ordinary
    workspace items (see test_phase10_corrected.py for the dedicated
    per-type API-shape assertions); this test just checks each type
    completes with the outcome shape appropriate to it.
    """
    package = fh.full_package()
    client = fh.make_client()
    dependency_ids: dict[str, str] = {}

    connection = fh.artifact_of(package, DeployableTargetType.CONNECTION)
    outcome = client.deploy_artifact(connection)
    assert outcome.status in ("created", "reused") and outcome.item_id
    dependency_ids[connection.artifact_id] = outcome.item_id

    lakehouse = fh.artifact_of(package, DeployableTargetType.LAKEHOUSE)
    outcome = client.deploy_artifact(lakehouse)
    assert outcome.status in ("created", "reused") and outcome.item_id
    dependency_ids[lakehouse.artifact_id] = outcome.item_id

    table = fh.artifact_of(package, DeployableTargetType.LAKEHOUSE_TABLE)
    outcome = client.deploy_artifact(table, dependency_ids)
    assert outcome.status == "deferred"
    assert outcome.materialization_status == "DEFERRED_TO_RUNTIME"
    assert outcome.item_id is None

    dataflow = fh.deployable_dataflow_artifact(package)
    outcome = client.deploy_artifact(dataflow)
    assert outcome.status in ("created", "reused") and outcome.item_id

    pipeline = fh.artifact_of(package, DeployableTargetType.DATA_PIPELINE)
    outcome = client.deploy_artifact(pipeline)
    assert outcome.status in ("created", "reused") and outcome.item_id
    dependency_ids[pipeline.artifact_id] = outcome.item_id

    schedule = fh.artifact_of(package, DeployableTargetType.SCHEDULE)
    outcome = client.deploy_artifact(schedule, dependency_ids)
    assert outcome.status in ("created", "reused") and outcome.item_id


def test_conflict_maps_code():
    package = fh.full_package()
    lakehouse = fh.artifact_of(package, DeployableTargetType.LAKEHOUSE)
    t = fh.FakeFabricTransport()
    t.force = ("/items", 409)
    with pytest.raises(FabricError) as exc:
        fh.make_client(transport=t).deploy_artifact(lakehouse)
    assert exc.value.code == CODE_CONFLICT


def test_schema_invalid_rejected_before_call():
    bad = GeneratedArtifact(
        artifact_id="lakehouse:bad",
        source_reference="x",
        target_type=DeployableTargetType.LAKEHOUSE,
        target_name="bad",
        generated_definition={"type": "Lakehouse"},  # missing name/properties
        content_digest="",
    )
    t = fh.FakeFabricTransport()
    client = fh.make_client(transport=t)
    with pytest.raises(FabricError) as exc:
        client.deploy_artifact(bad)
    assert exc.value.code == CODE_SCHEMA
    # No POST reached the transport.
    assert not any(m == "POST" for (m, *_rest) in t.calls)


# ── Throttling / timeout ─────────────────────────────────────────


def test_throttling_then_success():
    package = fh.full_package()
    lakehouse = fh.artifact_of(package, DeployableTargetType.LAKEHOUSE)
    t = fh.FakeFabricTransport()
    t.throttle_times = 2  # 429 twice, then create
    outcome = fh.make_client(transport=t).deploy_artifact(lakehouse)
    assert outcome.status == "created"


def test_persistent_throttling_maps_code():
    t = fh.FakeFabricTransport()
    t.force = (f"/workspaces/{fh.WS}", 429)
    with pytest.raises(FabricError) as exc:
        fh.make_client(transport=t, max_retries=2).verify_workspace()
    assert exc.value.code == CODE_THROTTLED


def test_timeout_maps_code():
    t = fh.FakeFabricTransport()
    t.timeout_always = True
    with pytest.raises(FabricError) as exc:
        fh.make_client(transport=t, max_retries=1).verify_workspace()
    assert exc.value.code == CODE_TIMEOUT


# ── Safety surface ───────────────────────────────────────────────


def test_no_delete_methods():
    client = fh.make_client()
    for attr in dir(client):
        low = attr.lower()
        for banned in ("delete", "remove", "drop", "purge", "destroy"):
            assert banned not in low, f"unexpected method: {attr}"


def test_secret_and_token_never_in_outcome():
    package = fh.full_package()
    lakehouse = fh.artifact_of(package, DeployableTargetType.LAKEHOUSE)
    outcome = fh.make_client().deploy_artifact(lakehouse)
    blob = repr(outcome)
    assert fh.FAKE_TOKEN not in blob
    assert "FABRIC-SECRET" not in blob


def test_disabled_by_default():
    class S:
        fabric_deployment_enabled = False

    with pytest.raises(FabricError) as exc:
        build_fabric_client_from_settings(S())
    assert exc.value.code == CODE_DISABLED
