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
    assert len([a for a in service._assets if a.asset_type == "activity"]) == 2
    assert len([a for a in service._assets if a.asset_type == "data_flow"]) == 1
    assert len([a for a in service._assets if a.asset_type == "dataset"]) == 6
    assert len([a for a in service._assets if a.asset_type == "linked_service"]) == 1
    assert len([a for a in service._assets if a.asset_type == "trigger"]) == 1


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
