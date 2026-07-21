"""Tests for sample CSV data quality — Phase 2."""

import json
from pathlib import Path

import pytest

from src.fixtures_loader import load_csv

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class TestOrdersData:
    @pytest.fixture
    def orders(self):
        return load_csv(FIXTURES / "data" / "orders.csv")

    def test_order_count_in_range(self, orders):
        assert 15 <= len(orders) <= 25

    def test_all_required_columns(self, orders):
        required = {"OrderId", "CustomerId", "ProductId", "Quantity", "DiscountPercent", "OrderDate"}
        for row in orders:
            assert required <= set(row.keys())

    def test_missing_customer_exists(self, orders):
        """At least one order references a customer not in customers.csv."""
        customers = load_csv(FIXTURES / "data" / "customers.csv")
        customer_ids = {r["CustomerId"] for r in customers}
        order_customer_ids = {r["CustomerId"] for r in orders}
        missing = order_customer_ids - customer_ids
        assert len(missing) > 0, "Expected at least one missing customer reference"

    def test_invalid_quantity_exists(self, orders):
        """At least one order has a non-positive quantity."""
        quantities = [int(r["Quantity"]) for r in orders]
        non_positive = [q for q in quantities if q <= 0]
        assert len(non_positive) > 0, "Expected at least one invalid quantity"

    def test_discounts_present(self, orders):
        """At least one order has a discount > 0."""
        discounts = [int(r["DiscountPercent"]) for r in orders]
        assert any(d > 0 for d in discounts)

    def test_multiple_regions(self, orders):
        """Orders span customers from multiple regions."""
        customers = load_csv(FIXTURES / "data" / "customers.csv")
        customer_regions = {r["CustomerId"]: r["Region"] for r in customers}
        regions_in_orders = {
            customer_regions.get(r["CustomerId"])
            for r in orders
            if r["CustomerId"] in customer_regions
        }
        assert len(regions_in_orders) >= 2


class TestCustomersData:
    @pytest.fixture
    def customers(self):
        return load_csv(FIXTURES / "data" / "customers.csv")

    def test_has_rows(self, customers):
        assert len(customers) > 0

    def test_has_inactive_customer(self, customers):
        inactive = [r for r in customers if r["IsActive"] == "false"]
        assert len(inactive) > 0


class TestProductsData:
    @pytest.fixture
    def products(self):
        return load_csv(FIXTURES / "data" / "products.csv")

    def test_has_rows(self, products):
        assert len(products) > 0

    def test_has_inactive_product(self, products):
        inactive = [r for r in products if r["IsActive"] == "false"]
        assert len(inactive) > 0

    def test_prices_are_positive(self, products):
        for row in products:
            assert float(row["UnitPrice"]) > 0


class TestNoSecretsInFixtures:
    """Verify no secrets exist anywhere in fixture files."""

    SECRETS_PATTERNS = [
        "password", "client_secret", "accountKey", "connectionString",
        "accessToken", "servicePrincipalKey",
    ]

    def test_no_secrets_in_adf_json(self):
        adf_root = FIXTURES / "adf"
        for json_file in adf_root.rglob("*.json"):
            content = json_file.read_text(encoding="utf-8")
            for pattern in self.SECRETS_PATTERNS:
                assert pattern not in content, (
                    f"Secret pattern '{pattern}' found in {json_file.name}"
                )

    def test_no_secrets_in_csv(self):
        data_root = FIXTURES / "data"
        for csv_file in data_root.glob("*.csv"):
            content = csv_file.read_text(encoding="utf-8")
            for pattern in self.SECRETS_PATTERNS:
                assert pattern not in content, (
                    f"Secret pattern '{pattern}' found in {csv_file.name}"
                )
