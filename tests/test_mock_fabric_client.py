"""Tests for the mock Fabric connector — Phase 7."""

import pytest

from src.connectors.mock_fabric_client import MockFabricClient, MockFabricError
from tests.package_helpers import make_package_plan


def generated_artifact():
    return make_package_plan().generated_package.artifacts[0]


def test_deterministic_ids():
    a = MockFabricClient()
    b = MockFabricClient()
    assert a.create_connection("ls_adls") == b.create_connection("ls_adls")
    assert a.create_table("enriched_orders") == "mock-table-enriched_orders"


def test_idempotent_creation_no_duplicates():
    client = MockFabricClient()
    id1 = client.create_lakehouse("lh")
    id2 = client.create_lakehouse("lh")
    assert id1 == id2
    assert client.resource_count() == 1


def test_resource_count_excludes_verify_and_run():
    client = MockFabricClient()
    client.verify_workspace("ws")
    client.run_target("pl")
    assert client.resource_count() == 0
    client.create_pipeline("pl")
    assert client.resource_count() == 1


def test_fail_on_action():
    client = MockFabricClient(fail_on_action="create_dataflow")
    client.create_connection("c")  # unaffected
    with pytest.raises(MockFabricError):
        client.create_dataflow("df")


def test_no_delete_methods():
    client = MockFabricClient()
    for attr in dir(client):
        assert "delete" not in attr.lower()
        assert "drop" not in attr.lower()
        assert "remove" not in attr.lower()


def test_has_resource():
    client = MockFabricClient()
    client.create_table("t")
    assert client.has_resource("table", "t")
    assert not client.has_resource("table", "other")


def test_deploys_and_stores_generated_definition():
    artifact = generated_artifact()
    client = MockFabricClient()
    resource_id = client.deploy_artifact(artifact)
    stored = client.get_deployed_artifact(artifact.artifact_id)
    assert resource_id.startswith("mock-connection-")
    assert stored["content_digest"] == artifact.content_digest
    assert stored["generated_definition"] == artifact.generated_definition


def test_definition_deployment_is_deterministic_and_idempotent():
    artifact = generated_artifact()
    first = MockFabricClient()
    second = MockFabricClient()
    id1 = first.deploy_artifact(artifact)
    id2 = first.deploy_artifact(artifact)
    id3 = second.deploy_artifact(artifact)
    assert id1 == id2 == id3
    assert first.resource_count() == 1


def test_definition_failure_injection():
    artifact = generated_artifact()
    client = MockFabricClient(fail_on_action="create_connection")
    with pytest.raises(MockFabricError, match="Injected failure"):
        client.deploy_artifact(artifact)
