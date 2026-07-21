"""Tests for Pydantic domain models — Phase 2."""

import json
from pathlib import Path

import pytest

from src.models.schemas import (
    ADFInventory,
    ADFPipeline,
    AssetReference,
    DataFlowSink,
    DataFlowSource,
    DataFlowTransformation,
    Dataset,
    LinkedService,
    MappingDataFlow,
    PipelineActivity,
    Trigger,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# ── AssetReference ───────────────────────────────────────────────


class TestAssetReference:
    def test_create_with_alias(self):
        ref = AssetReference(referenceName="ls_adls", type="LinkedServiceReference")
        assert ref.reference_name == "ls_adls"

    def test_create_with_field_name(self):
        ref = AssetReference(reference_name="ds_orders", type="DatasetReference")
        assert ref.reference_name == "ds_orders"

    def test_serialize_uses_alias(self):
        ref = AssetReference(reference_name="ls_adls", type="LinkedServiceReference")
        data = ref.model_dump(by_alias=True)
        assert "referenceName" in data


# ── LinkedService ────────────────────────────────────────────────


class TestLinkedService:
    def test_parse_fixture(self):
        path = FIXTURES / "adf" / "linked_services" / "ls_adls.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        ls = LinkedService(**raw)
        assert ls.name == "ls_adls"
        assert ls.properties.type == "AzureBlobFS"

    def test_extra_fields_allowed(self):
        data = {
            "name": "test_ls",
            "properties": {"type": "AzureBlobStorage", "unknownField": 123},
            "etag": "abc",
        }
        ls = LinkedService(**data)
        assert ls.name == "test_ls"

    def test_json_roundtrip(self):
        path = FIXTURES / "adf" / "linked_services" / "ls_adls.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        ls = LinkedService(**raw)
        serialized = json.loads(ls.model_dump_json(by_alias=True))
        assert serialized["name"] == "ls_adls"


# ── Dataset ──────────────────────────────────────────────────────


class TestDataset:
    def test_parse_orders_fixture(self):
        path = FIXTURES / "adf" / "datasets" / "ds_orders.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        ds = Dataset(**raw)
        assert ds.name == "ds_orders"
        assert ds.properties.type == "DelimitedText"

    def test_dataset_references_linked_service(self):
        path = FIXTURES / "adf" / "datasets" / "ds_orders.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        ds = Dataset(**raw)
        assert ds.properties.linked_service_name is not None
        assert ds.properties.linked_service_name.reference_name == "ls_adls"

    def test_all_datasets_parse(self):
        ds_dir = FIXTURES / "adf" / "datasets"
        for f in ds_dir.glob("*.json"):
            raw = json.loads(f.read_text(encoding="utf-8"))
            ds = Dataset(**raw)
            assert ds.name, f"Dataset in {f.name} has no name"

    def test_json_roundtrip(self):
        path = FIXTURES / "adf" / "datasets" / "ds_customers.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        ds = Dataset(**raw)
        serialized = json.loads(ds.model_dump_json(by_alias=True))
        assert serialized["name"] == "ds_customers"


# ── MappingDataFlow ──────────────────────────────────────────────


class TestMappingDataFlow:
    @pytest.fixture
    def data_flow(self):
        path = FIXTURES / "adf" / "dataflows" / "df_sales_processing_legacy.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        return MappingDataFlow(**raw)

    def test_parse_fixture(self, data_flow):
        assert data_flow.name == "df_sales_processing_legacy"

    def test_has_three_sources(self, data_flow):
        sources = data_flow.properties.type_properties.sources
        assert len(sources) == 3

    def test_has_three_sinks(self, data_flow):
        sinks = data_flow.properties.type_properties.sinks
        assert len(sinks) == 3

    def test_source_names(self, data_flow):
        names = {s.name for s in data_flow.properties.type_properties.sources}
        assert names == {"SourceOrders", "SourceCustomers", "SourceProducts"}

    def test_sink_names(self, data_flow):
        names = {s.name for s in data_flow.properties.type_properties.sinks}
        assert names == {"SinkEnrichedOrders", "SinkRejectedOrders", "SinkCustomerSummary"}

    def test_join_transformations_exist(self, data_flow):
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

    def test_has_script_lines(self, data_flow):
        lines = data_flow.properties.type_properties.script_lines
        assert lines is not None
        assert len(lines) > 0

    def test_json_roundtrip(self, data_flow):
        serialized = json.loads(data_flow.model_dump_json(by_alias=True))
        reparsed = MappingDataFlow(**serialized)
        assert reparsed.name == data_flow.name
        assert len(reparsed.properties.type_properties.sources) == 3


# ── PipelineActivity & ADFPipeline ───────────────────────────────


class TestADFPipeline:
    @pytest.fixture
    def pipeline(self):
        path = FIXTURES / "adf" / "pipelines" / "pl_sales_processing_legacy.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        return ADFPipeline(**raw)

    def test_parse_fixture(self, pipeline):
        assert pipeline.name == "pl_sales_processing_legacy"

    def test_activity_count(self, pipeline):
        assert len(pipeline.properties.activities) == 2

    def test_activity_types(self, pipeline):
        types = {a.type for a in pipeline.properties.activities}
        assert "GetMetadata" in types
        assert "IfCondition" in types

    def test_pipeline_references_data_flow(self, pipeline):
        """The IfCondition activity should reference df_sales_processing_legacy."""
        raw = json.loads(
            (FIXTURES / "adf" / "pipelines" / "pl_sales_processing_legacy.json")
            .read_text(encoding="utf-8")
        )
        raw_str = json.dumps(raw)
        assert "df_sales_processing_legacy" in raw_str

    def test_dependency_chain(self, pipeline):
        if_cond = next(
            a for a in pipeline.properties.activities if a.type == "IfCondition"
        )
        assert len(if_cond.depends_on) > 0
        assert if_cond.depends_on[0].activity == "GetOrdersMetadata"

    def test_has_parameters(self, pipeline):
        assert pipeline.properties.parameters is not None
        assert "RunDate" in pipeline.properties.parameters

    def test_json_roundtrip(self, pipeline):
        serialized = json.loads(pipeline.model_dump_json(by_alias=True))
        reparsed = ADFPipeline(**serialized)
        assert reparsed.name == pipeline.name


# ── Trigger ──────────────────────────────────────────────────────


class TestTrigger:
    def test_parse_fixture(self):
        path = FIXTURES / "adf" / "triggers" / "trg_daily_sales.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        trg = Trigger(**raw)
        assert trg.name == "trg_daily_sales"
        assert trg.properties.type == "ScheduleTrigger"

    def test_trigger_references_pipeline(self):
        path = FIXTURES / "adf" / "triggers" / "trg_daily_sales.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        trg = Trigger(**raw)
        assert len(trg.properties.pipelines) > 0
        ref = trg.properties.pipelines[0].pipeline_reference
        assert ref.reference_name == "pl_sales_processing_legacy"

    def test_json_roundtrip(self):
        path = FIXTURES / "adf" / "triggers" / "trg_daily_sales.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        trg = Trigger(**raw)
        serialized = json.loads(trg.model_dump_json(by_alias=True))
        assert serialized["name"] == "trg_daily_sales"


# ── ADFInventory ─────────────────────────────────────────────────


class TestADFInventory:
    def test_empty_inventory(self):
        inv = ADFInventory()
        assert len(inv.pipelines) == 0
        assert len(inv.datasets) == 0

    def test_inventory_with_data(self):
        inv = ADFInventory(
            pipelines=[
                ADFPipeline(
                    name="test",
                    properties={"activities": []},
                )
            ]
        )
        assert len(inv.pipelines) == 1
