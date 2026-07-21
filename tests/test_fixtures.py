"""Tests for fixture loader — Phase 2."""

import json
import os
from pathlib import Path

import pytest

from src.fixtures_loader import load_csv, load_json, load_mock_adf_inventory

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# ── load_json ────────────────────────────────────────────────────


class TestLoadJson:
    def test_loads_valid_json(self):
        path = FIXTURES / "adf" / "linked_services" / "ls_adls.json"
        data = load_json(path)
        assert data is not None
        assert data["name"] == "ls_adls"

    def test_returns_none_for_missing_file(self):
        data = load_json(Path("nonexistent.json"))
        assert data is None

    def test_returns_none_for_malformed_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{invalid json", encoding="utf-8")
        data = load_json(bad)
        assert data is None

    def test_raises_on_credential_key(self, tmp_path):
        cred = tmp_path / "cred.json"
        cred.write_text(
            json.dumps({"name": "bad", "properties": {"password": "secret123"}}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Credential-like key"):
            load_json(cred)

    def test_all_adf_fixtures_load(self):
        """Every JSON file in fixtures/adf/ must load without error."""
        adf_root = FIXTURES / "adf"
        count = 0
        for json_file in adf_root.rglob("*.json"):
            data = load_json(json_file)
            assert data is not None, f"Failed to load {json_file}"
            count += 1
        assert count > 0


# ── load_csv ─────────────────────────────────────────────────────


class TestLoadCsv:
    def test_loads_orders_csv(self):
        rows = load_csv(FIXTURES / "data" / "orders.csv")
        assert rows is not None
        assert len(rows) > 0

    def test_orders_headers_correct(self):
        rows = load_csv(FIXTURES / "data" / "orders.csv")
        expected = {"OrderId", "CustomerId", "ProductId", "Quantity", "DiscountPercent", "OrderDate"}
        assert expected == set(rows[0].keys())

    def test_customers_headers_correct(self):
        rows = load_csv(FIXTURES / "data" / "customers.csv")
        expected = {"CustomerId", "CustomerName", "Region", "IsActive"}
        assert expected == set(rows[0].keys())

    def test_products_headers_correct(self):
        rows = load_csv(FIXTURES / "data" / "products.csv")
        expected = {"ProductId", "ProductName", "Category", "UnitPrice", "IsActive"}
        assert expected == set(rows[0].keys())

    def test_returns_none_for_missing_file(self):
        rows = load_csv(Path("nonexistent.csv"))
        assert rows is None


# ── load_mock_adf_inventory ──────────────────────────────────────


class TestLoadMockAdfInventory:
    @pytest.fixture
    def inventory(self):
        return load_mock_adf_inventory(FIXTURES)

    def test_loads_successfully(self, inventory):
        assert inventory is not None

    def test_pipeline_count(self, inventory):
        assert len(inventory.pipelines) == 1

    def test_linked_service_count(self, inventory):
        assert len(inventory.linked_services) == 1

    def test_dataset_count(self, inventory):
        assert len(inventory.datasets) == 6

    def test_data_flow_count(self, inventory):
        assert len(inventory.data_flows) == 1

    def test_trigger_count(self, inventory):
        assert len(inventory.triggers) == 1

    def test_raises_for_missing_root(self):
        with pytest.raises(FileNotFoundError):
            load_mock_adf_inventory(Path("nonexistent_dir"))

    def test_raises_for_missing_required_dirs(self, tmp_path):
        (tmp_path / "adf").mkdir()
        with pytest.raises(FileNotFoundError, match="Required fixture directory"):
            load_mock_adf_inventory(tmp_path)
