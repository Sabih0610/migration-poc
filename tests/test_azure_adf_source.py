"""Tests for Azure→internal conversion and source compatibility."""

from pathlib import Path

import pytest

from src.connectors.adf_source import FixtureADFSource
from src.connectors.azure_adf_client import CODE_DISABLED, CODE_MALFORMED, AzureDiscoveryError
from src.connectors.azure_adf_source import (
    AzureADFSource,
    build_azure_adf_client_from_settings,
    convert_raw_to_inventory,
)
from src.migration.discovery import ADFDiscoveryService
from tests import azure_helpers as az

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def test_convert_raw_matches_fixture_counts():
    inv = convert_raw_to_inventory(az.fixture_raw_definitions())
    assert len(inv.pipelines) == 1
    assert len(inv.data_flows) == 1
    assert len(inv.datasets) == 6
    assert len(inv.linked_services) == 1
    assert len(inv.triggers) == 1


def test_azure_source_interface_compatible_with_fixture():
    """Azure discovery yields the same DiscoveryResult shape as fixtures."""
    fixture_inv = FixtureADFSource(FIXTURES).load_inventory()
    azure_inv = AzureADFSource(az.make_client()).load_inventory()

    fixture_result = ADFDiscoveryService(fixture_inv).scan_inventory()
    azure_result = ADFDiscoveryService(azure_inv).scan_inventory()

    assert azure_result.summary.model_dump() == fixture_result.summary.model_dump()
    assert {a.asset_name for a in azure_result.assets} == {
        a.asset_name for a in fixture_result.assets
    }
    assert len(azure_result.missing_dependencies) == 0


def test_azure_source_preserves_nested_and_definitions():
    inv = AzureADFSource(az.make_client()).load_inventory()
    pipeline = inv.pipelines[0]
    # Nested IfCondition preserved in the internal model.
    types = {a.type for a in pipeline.properties.activities}
    assert "IfCondition" in types
    # Data flow transformations preserved with order.
    df = inv.data_flows[0]
    names = [t.name for t in df.properties.type_properties.transformations]
    assert names[0] == "CastOrderTypes"
    assert "AggregateByCustomerRegion" in names


def test_build_client_disabled_by_default():
    class S:
        enable_azure_discovery = False

    with pytest.raises(AzureDiscoveryError) as exc:
        build_azure_adf_client_from_settings(S())
    assert exc.value.code == CODE_DISABLED


def test_convert_invalid_definition_raises_malformed():
    with pytest.raises(AzureDiscoveryError) as exc:
        convert_raw_to_inventory({"pipelines": [{"name": "p"}]})  # missing properties
    assert exc.value.code == CODE_MALFORMED
