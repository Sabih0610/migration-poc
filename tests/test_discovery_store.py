"""Persistence tests for lossless discovery snapshots."""

import json
from pathlib import Path

from src.fixtures_loader import load_mock_adf_inventory
from src.migration.discovery import ADFDiscoveryService
from src.migration.discovery_store import (
    get_discovery,
    get_latest_discovery,
    list_discoveries,
    save_discovery,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def test_discovery_snapshot_round_trip_is_lossless():
    inventory = load_mock_adf_inventory(FIXTURES)
    result = ADFDiscoveryService(inventory).scan_inventory()
    saved = save_discovery(result)
    loaded = get_discovery(saved["id"])

    assert loaded is not None
    assert loaded["artifact_count"] == 10
    assert loaded["component_count"] == 11
    assert loaded["result"].inventory.source_definitions == (
        inventory.source_definitions
    )
    assert json.loads(
        loaded["result"].model_dump_json(by_alias=True)
    )["inventory"]["source_definitions"] == inventory.source_definitions


def test_latest_and_list_discoveries():
    inventory = load_mock_adf_inventory(FIXTURES)
    result = ADFDiscoveryService(inventory).scan_inventory()
    first = save_discovery(result)
    second = save_discovery(result)

    assert get_latest_discovery()["id"] == second["id"]
    assert [item["id"] for item in list_discoveries()] == [
        second["id"],
        first["id"],
    ]


def test_unknown_discovery_returns_none():
    assert get_discovery(999999) is None
