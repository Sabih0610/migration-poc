"""Phase 2 integration tests — cross-reference validation and negative cases.

Tests that the full ADF fixture set is internally consistent:
references resolve, negative cases fail correctly, and no secrets leak.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.fixtures_loader import load_csv, load_json, load_mock_adf_inventory
from src.models.schemas import (
    ADFInventory,
    ADFPipeline,
    AssetReference,
    Dataset,
    LinkedService,
    MappingDataFlow,
    PipelineActivity,
    Trigger,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# ── Cross-reference validation ───────────────────────────────────


class TestCrossReferences:
    """Validate that all inter-asset references in fixtures resolve."""

    @pytest.fixture
    def inventory(self):
        return load_mock_adf_inventory(FIXTURES)

    def test_all_datasets_reference_existing_linked_service(self, inventory):
        ls_names = {ls.name for ls in inventory.linked_services}
        for ds in inventory.datasets:
            ref = ds.properties.linked_service_name
            if ref is not None:
                assert ref.reference_name in ls_names, (
                    f"Dataset '{ds.name}' references linked service "
                    f"'{ref.reference_name}' which does not exist. "
                    f"Available: {ls_names}"
                )

    def test_all_dataflow_sources_reference_existing_datasets(self, inventory):
        ds_names = {ds.name for ds in inventory.datasets}
        for df in inventory.data_flows:
            for src in df.properties.type_properties.sources:
                if src.dataset is not None:
                    assert src.dataset.reference_name in ds_names, (
                        f"Data flow source '{src.name}' references dataset "
                        f"'{src.dataset.reference_name}' which does not exist."
                    )

    def test_all_dataflow_sinks_reference_existing_datasets(self, inventory):
        ds_names = {ds.name for ds in inventory.datasets}
        for df in inventory.data_flows:
            for sink in df.properties.type_properties.sinks:
                if sink.dataset is not None:
                    assert sink.dataset.reference_name in ds_names, (
                        f"Data flow sink '{sink.name}' references dataset "
                        f"'{sink.dataset.reference_name}' which does not exist."
                    )

    def test_pipeline_references_valid_data_flow(self, inventory):
        """Pipeline's ExecuteDataFlow must reference an existing data flow."""
        df_names = {df.name for df in inventory.data_flows}
        for pipeline in inventory.pipelines:
            raw_str = pipeline.model_dump_json(by_alias=True)
            for df_name in df_names:
                if df_name in raw_str:
                    break
            else:
                # Check raw fixture JSON for nested references
                path = FIXTURES / "adf" / "pipelines" / f"{pipeline.name}.json"
                if path.exists():
                    content = path.read_text(encoding="utf-8")
                    found = any(name in content for name in df_names)
                    assert found, (
                        f"Pipeline '{pipeline.name}' does not reference "
                        f"any known data flow. Available: {df_names}"
                    )

    def test_trigger_references_valid_pipeline(self, inventory):
        pl_names = {pl.name for pl in inventory.pipelines}
        for trg in inventory.triggers:
            for pipe_ref in trg.properties.pipelines:
                if pipe_ref.pipeline_reference is not None:
                    assert pipe_ref.pipeline_reference.reference_name in pl_names, (
                        f"Trigger '{trg.name}' references pipeline "
                        f"'{pipe_ref.pipeline_reference.reference_name}' "
                        f"which does not exist. Available: {pl_names}"
                    )

    def test_pipeline_activities_reference_existing_datasets(self, inventory):
        """GetMetadata activity should reference an existing dataset."""
        ds_names = {ds.name for ds in inventory.datasets}
        for pipeline in inventory.pipelines:
            raw = json.loads(
                (FIXTURES / "adf" / "pipelines" / f"{pipeline.name}.json")
                .read_text(encoding="utf-8")
            )
            raw_str = json.dumps(raw)
            # At least one dataset should be referenced
            found = any(name in raw_str for name in ds_names)
            assert found, (
                f"Pipeline '{pipeline.name}' does not reference any dataset."
            )

    def test_no_unresolved_references(self, inventory):
        """Collect all references and verify they all resolve."""
        ls_names = {ls.name for ls in inventory.linked_services}
        ds_names = {ds.name for ds in inventory.datasets}
        df_names = {df.name for df in inventory.data_flows}
        pl_names = {pl.name for pl in inventory.pipelines}

        unresolved = []

        # Dataset → LinkedService
        for ds in inventory.datasets:
            ref = ds.properties.linked_service_name
            if ref and ref.reference_name not in ls_names:
                unresolved.append(f"Dataset '{ds.name}' → LS '{ref.reference_name}'")

        # DataFlow sources → Datasets
        for df in inventory.data_flows:
            for src in df.properties.type_properties.sources:
                if src.dataset and src.dataset.reference_name not in ds_names:
                    unresolved.append(f"DF source '{src.name}' → DS '{src.dataset.reference_name}'")

        # DataFlow sinks → Datasets
        for df in inventory.data_flows:
            for sink in df.properties.type_properties.sinks:
                if sink.dataset and sink.dataset.reference_name not in ds_names:
                    unresolved.append(f"DF sink '{sink.name}' → DS '{sink.dataset.reference_name}'")

        # Trigger → Pipeline
        for trg in inventory.triggers:
            for pipe_ref in trg.properties.pipelines:
                if pipe_ref.pipeline_reference:
                    if pipe_ref.pipeline_reference.reference_name not in pl_names:
                        unresolved.append(
                            f"Trigger '{trg.name}' → PL "
                            f"'{pipe_ref.pipeline_reference.reference_name}'"
                        )

        assert unresolved == [], f"Unresolved references: {unresolved}"


# ── Duplicate detection ──────────────────────────────────────────


class TestDuplicateDetection:
    @pytest.fixture
    def inventory(self):
        return load_mock_adf_inventory(FIXTURES)

    def test_no_duplicate_pipeline_names(self, inventory):
        names = [p.name for p in inventory.pipelines]
        assert len(names) == len(set(names)), f"Duplicate pipelines: {names}"

    def test_no_duplicate_dataset_names(self, inventory):
        names = [d.name for d in inventory.datasets]
        assert len(names) == len(set(names)), f"Duplicate datasets: {names}"

    def test_no_duplicate_linked_service_names(self, inventory):
        names = [ls.name for ls in inventory.linked_services]
        assert len(names) == len(set(names)), f"Duplicate linked services: {names}"

    def test_no_duplicate_dataflow_names(self, inventory):
        names = [df.name for df in inventory.data_flows]
        assert len(names) == len(set(names)), f"Duplicate data flows: {names}"

    def test_no_duplicate_trigger_names(self, inventory):
        names = [t.name for t in inventory.triggers]
        assert len(names) == len(set(names)), f"Duplicate triggers: {names}"

    def test_no_duplicate_order_ids(self):
        rows = load_csv(FIXTURES / "data" / "orders.csv")
        ids = [r["OrderId"] for r in rows]
        assert len(ids) == len(set(ids)), f"Duplicate OrderIds found"

    def test_no_duplicate_customer_ids(self):
        rows = load_csv(FIXTURES / "data" / "customers.csv")
        ids = [r["CustomerId"] for r in rows]
        assert len(ids) == len(set(ids)), f"Duplicate CustomerIds found"

    def test_no_duplicate_product_ids(self):
        rows = load_csv(FIXTURES / "data" / "products.csv")
        ids = [r["ProductId"] for r in rows]
        assert len(ids) == len(set(ids)), f"Duplicate ProductIds found"


# ── Negative / failure cases ─────────────────────────────────────


class TestNegativeCases:
    def test_malformed_json_returns_none(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{ broken }", encoding="utf-8")
        result = load_json(bad)
        assert result is None

    def test_missing_file_returns_none(self):
        result = load_json(Path("does_not_exist.json"))
        assert result is None

    def test_missing_csv_returns_none(self):
        result = load_csv(Path("does_not_exist.csv"))
        assert result is None

    def test_missing_required_name_fails(self):
        """Pipeline without required 'name' field should fail validation."""
        with pytest.raises(ValidationError):
            ADFPipeline(properties={"activities": []})

    def test_missing_required_properties_fails(self):
        """Pipeline without 'properties' should fail validation."""
        with pytest.raises(ValidationError):
            ADFPipeline(name="test")

    def test_linked_service_missing_name_fails(self):
        with pytest.raises(ValidationError):
            LinkedService(properties={"type": "AzureBlobFS"})

    def test_dataset_missing_name_fails(self):
        with pytest.raises(ValidationError):
            Dataset(properties={"type": "DelimitedText"})

    def test_trigger_missing_name_fails(self):
        with pytest.raises(ValidationError):
            Trigger(properties={"type": "ScheduleTrigger"})

    def test_dataflow_missing_name_fails(self):
        with pytest.raises(ValidationError):
            MappingDataFlow(properties={"type": "MappingDataFlow", "typeProperties": {"sources": []}})

    def test_credential_key_in_nested_structure_fails(self, tmp_path):
        nested = tmp_path / "nested.json"
        nested.write_text(
            json.dumps({
                "name": "bad_ls",
                "properties": {
                    "type": "AzureSql",
                    "typeProperties": {
                        "connectionString": "Server=x;Password=y"
                    }
                }
            }),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Credential-like key"):
            load_json(nested)

    def test_credential_key_account_key_fails(self, tmp_path):
        f = tmp_path / "ak.json"
        f.write_text(
            json.dumps({"name": "x", "properties": {"accountKey": "abc123"}}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Credential-like key"):
            load_json(f)

    def test_broken_reference_detected(self):
        """Inventory with a dataset referencing a nonexistent linked service."""
        inv = ADFInventory(
            linked_services=[
                LinkedService(name="ls_real", properties={"type": "AzureBlobFS"})
            ],
            datasets=[
                Dataset(
                    name="ds_broken",
                    properties={
                        "type": "DelimitedText",
                        "linkedServiceName": {
                            "referenceName": "ls_nonexistent",
                            "type": "LinkedServiceReference",
                        },
                    },
                )
            ],
        )
        ls_names = {ls.name for ls in inv.linked_services}
        broken = [
            ds.name
            for ds in inv.datasets
            if ds.properties.linked_service_name
            and ds.properties.linked_service_name.reference_name not in ls_names
        ]
        assert "ds_broken" in broken


# ── Secret scanning ──────────────────────────────────────────────


class TestNoSecretsInInventory:
    """Verify the loaded inventory contains no secret values."""

    SENSITIVE_PATTERNS = [
        "password", "client_secret", "accountKey", "account_key",
        "connectionString", "connection_string", "accessToken",
        "access_token", "servicePrincipalKey", "service_principal_key",
    ]

    def test_no_secrets_in_serialized_inventory(self):
        inv = load_mock_adf_inventory(FIXTURES)
        serialized = inv.model_dump_json(by_alias=True)
        for pattern in self.SENSITIVE_PATTERNS:
            assert pattern not in serialized, (
                f"Secret pattern '{pattern}' found in serialized inventory"
            )

    def test_no_secrets_in_raw_fixture_files(self):
        for json_file in (FIXTURES / "adf").rglob("*.json"):
            content = json_file.read_text(encoding="utf-8")
            for pattern in self.SENSITIVE_PATTERNS:
                assert pattern not in content, (
                    f"Secret pattern '{pattern}' found in {json_file.name}"
                )


# ── Data flow structure deep validation ──────────────────────────


class TestDataFlowStructure:
    """Deep structural validation of the mapping data flow."""

    @pytest.fixture
    def data_flow(self):
        inv = load_mock_adf_inventory(FIXTURES)
        return inv.data_flows[0]

    def test_exactly_three_sources(self, data_flow):
        assert len(data_flow.properties.type_properties.sources) == 3

    def test_exactly_three_sinks(self, data_flow):
        assert len(data_flow.properties.type_properties.sinks) == 3

    def test_seven_transformations(self, data_flow):
        assert len(data_flow.properties.type_properties.transformations) == 7

    def test_joins_exist(self, data_flow):
        names = {t.name for t in data_flow.properties.type_properties.transformations}
        assert "JoinCustomers" in names
        assert "JoinProducts" in names

    def test_derived_column_exists(self, data_flow):
        names = {t.name for t in data_flow.properties.type_properties.transformations}
        assert "DerivedColumns" in names

    def test_conditional_split_exists(self, data_flow):
        names = {t.name for t in data_flow.properties.type_properties.transformations}
        assert "ConditionalSplitValidRejected" in names

    def test_aggregation_exists(self, data_flow):
        names = {t.name for t in data_flow.properties.type_properties.transformations}
        assert "AggregateByCustomerRegion" in names

    def test_script_lines_reference_all_sources(self, data_flow):
        lines = "\n".join(data_flow.properties.type_properties.script_lines or [])
        assert "SourceOrders" in lines
        assert "SourceCustomers" in lines
        assert "SourceProducts" in lines

    def test_script_lines_reference_all_sinks(self, data_flow):
        lines = "\n".join(data_flow.properties.type_properties.script_lines or [])
        assert "SinkEnrichedOrders" in lines
        assert "SinkRejectedOrders" in lines
        assert "SinkCustomerSummary" in lines


# ── CSV validation ───────────────────────────────────────────────


class TestCSVIntegrity:
    def test_orders_row_count_nonzero(self):
        rows = load_csv(FIXTURES / "data" / "orders.csv")
        assert rows is not None and len(rows) > 0

    def test_customers_row_count_nonzero(self):
        rows = load_csv(FIXTURES / "data" / "customers.csv")
        assert rows is not None and len(rows) > 0

    def test_products_row_count_nonzero(self):
        rows = load_csv(FIXTURES / "data" / "products.csv")
        assert rows is not None and len(rows) > 0

    def test_orders_exact_headers(self):
        rows = load_csv(FIXTURES / "data" / "orders.csv")
        expected = ["OrderId", "CustomerId", "ProductId", "Quantity", "DiscountPercent", "OrderDate"]
        assert list(rows[0].keys()) == expected

    def test_customers_exact_headers(self):
        rows = load_csv(FIXTURES / "data" / "customers.csv")
        expected = ["CustomerId", "CustomerName", "Region", "IsActive"]
        assert list(rows[0].keys()) == expected

    def test_products_exact_headers(self):
        rows = load_csv(FIXTURES / "data" / "products.csv")
        expected = ["ProductId", "ProductName", "Category", "UnitPrice", "IsActive"]
        assert list(rows[0].keys()) == expected

    def test_intended_invalid_orders_exist(self):
        """Verify intentional data quality issues in orders."""
        rows = load_csv(FIXTURES / "data" / "orders.csv")
        quantities = [int(r["Quantity"]) for r in rows]
        # Negative quantity
        assert any(q < 0 for q in quantities), "No negative quantity found"
        # Zero quantity
        assert any(q == 0 for q in quantities), "No zero quantity found"

    def test_inactive_product_referenced_in_orders(self):
        """At least one order references an inactive product."""
        orders = load_csv(FIXTURES / "data" / "orders.csv")
        products = load_csv(FIXTURES / "data" / "products.csv")
        inactive_ids = {
            r["ProductId"] for r in products if r["IsActive"] == "false"
        }
        order_product_ids = {r["ProductId"] for r in orders}
        overlap = inactive_ids & order_product_ids
        assert len(overlap) > 0, "No orders reference inactive products"
