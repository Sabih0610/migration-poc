"""Tests for the discovery service — Phase 3."""

import pytest
from pathlib import Path

from src.fixtures_loader import load_mock_adf_inventory
from src.migration.discovery import ADFDiscoveryService
from src.models.schemas import (
    ADFInventory,
    AssetReference,
    Trigger,
    TriggerPipelineRef,
    TriggerProperties,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def inventory():
    return load_mock_adf_inventory(FIXTURES)


@pytest.fixture
def service(inventory):
    return ADFDiscoveryService(inventory)


def test_scan_inventory(service):
    result = service.scan_inventory()
    assert result is not None
    assert len(result.assets) > 0
    assert len(result.dependencies) > 0
    assert result.summary.pipeline_count == 1
    assert result.summary.data_flow_count == 1
    assert result.summary.dataset_count == 6
    assert result.summary.linked_service_count == 1
    assert result.summary.trigger_count == 1
    assert result.summary.artifact_count == 10
    assert result.summary.component_count == 11
    assert result.summary.activity_count == 4
    assert result.summary.transformation_count == 7
    assert result.summary.expression_count == 2
    assert result.summary.connection_reference_count == 6
    assert len(result.components) == 11


def test_discover_assets(service):
    service.discover_assets()
    asset_types = {a.asset_type for a in service._assets}
    assert "pipeline" in asset_types
    assert "activity" in asset_types
    assert "data_flow" in asset_types
    assert "dataset" in asset_types
    assert "linked_service" in asset_types
    assert "trigger" in asset_types

    # Exact counts based on fixtures
    assert len([a for a in service._assets if a.asset_type == "pipeline"]) == 1
    assert len([a for a in service._assets if a.asset_type == "activity"]) == 4
    assert len([a for a in service._assets if a.asset_type == "data_flow"]) == 1
    assert len([a for a in service._assets if a.asset_type == "dataset"]) == 6
    assert len([a for a in service._assets if a.asset_type == "linked_service"]) == 1
    assert len([a for a in service._assets if a.asset_type == "trigger"]) == 1
    assert len(service._components) == 11


def test_complete_definitions_and_nested_components_preserved(inventory):
    result = ADFDiscoveryService(inventory).scan_inventory()
    pipeline = next(a for a in result.assets if a.asset_type == "pipeline")
    assert pipeline.definition == inventory.source_definitions["pipelines"][0]
    assert pipeline.definition["properties"]["parameters"]["RunDate"]["type"] == "String"
    assert pipeline.definition["properties"]["variables"]["ProcessingStatus"]["defaultValue"] == "pending"

    activities = [c for c in result.components if c.component_type == "activity"]
    transformations = [
        c for c in result.components if c.component_type == "transformation"
    ]
    assert {c.component_name for c in activities} == {
        "GetOrdersMetadata",
        "CheckFileExists",
        "ExecuteSalesDataFlow",
        "FailMissingFile",
    }
    assert len(transformations) == 7
    assert any("ifTrueActivities" in c.property_path for c in activities)
    assert all(a.is_component for a in result.assets if a.asset_type == "activity")


def test_expressions_and_connection_references(inventory):
    result = ADFDiscoveryService(inventory).scan_inventory()
    values = {expression.value for expression in result.expressions}
    assert "@activity('GetOrdersMetadata').output.exists" in values
    assert "@trigger().scheduledTime" in values
    assert len(result.connection_references) == 6
    assert {ref.connection_name for ref in result.connection_references} == {
        "ls_adls"
    }


def test_missing_dependency_detection():
    # Create inventory with a missing pipeline ref from a trigger
    inv = ADFInventory(
        pipelines=[],
        triggers=[
            Trigger(
                name="trg1",
                properties=TriggerProperties(
                    type="ScheduleTrigger",
                    pipelines=[
                        TriggerPipelineRef(
                            pipeline_reference=AssetReference(
                                referenceName="missing_pl", type="PipelineReference"
                            )
                        )
                    ],
                ),
            )
        ],
    )
    svc = ADFDiscoveryService(inv)
    res = svc.scan_inventory()
    assert len(res.missing_dependencies) == 1
    assert res.missing_dependencies[0].missing_reference == "missing_pl"
