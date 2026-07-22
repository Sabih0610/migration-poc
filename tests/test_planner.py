"""Tests for the Phase 5 migration planner."""

import json
from pathlib import Path

import pytest

from src.fixtures_loader import load_mock_adf_inventory
from src.migration.assessment import ADFCompatibilityAssessment
from src.migration.discovery import ADFDiscoveryService
from src.migration.planner import MigrationPlanner
from src.models.schemas import (
    ADFInventory,
    ADFPipeline,
    AssessmentStatus,
    AssetReference,
    Dataset,
    DatasetProperties,
    MigrationActionType,
    MigrationRisk,
    PipelineActivity,
    PipelineProperties,
    TargetItemType,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

EXPECTED_ORDER = [
    "verify_workspace",
    "create_connection",
    "create_lakehouse",
    "create_table",
    "create_table",
    "create_table",
    "create_dataflow",
    "create_pipeline",
    "configure_schedule",
    "run_target",
    "validate",
]


@pytest.fixture
def plan():
    inv = load_mock_adf_inventory(FIXTURES)
    disc = ADFDiscoveryService(inv).scan_inventory()
    assess = ADFCompatibilityAssessment(inv).assess_discovery(disc)
    return MigrationPlanner(inv).generate_plan(disc, assess)


def _target_of(plan, source_asset):
    return next(
        m.target_item_type for m in plan.mappings if m.source_asset == source_asset
    )


# ── Mapping coverage ─────────────────────────────────────────────


def test_all_assets_mapped_or_explained(plan):
    assert len(plan.mappings) == 14
    for mapping in plan.mappings:
        # Every asset is either mapped to a target or has an explanation.
        assert mapping.mapped or mapping.explanation


def test_pipeline_maps_to_fabric_pipeline(plan):
    assert (
        _target_of(plan, "pl_sales_processing_legacy")
        == TargetItemType.DATA_PIPELINE
    )


def test_data_flow_maps_to_dataflow_gen2(plan):
    assert (
        _target_of(plan, "df_sales_processing_legacy")
        == TargetItemType.DATAFLOW_GEN2
    )


def test_linked_service_maps_to_connection(plan):
    assert _target_of(plan, "ls_adls") == TargetItemType.CONNECTION


def test_sink_datasets_map_to_lakehouse_tables(plan):
    for sink in ("ds_enriched_orders", "ds_customer_summary", "ds_rejected_orders"):
        assert _target_of(plan, sink) == TargetItemType.LAKEHOUSE_TABLE


def test_trigger_maps_to_schedule(plan):
    assert _target_of(plan, "trg_daily_sales") == TargetItemType.SCHEDULE


# ── Actions & ordering ───────────────────────────────────────────


def test_deployment_order_correct(plan):
    types = [a.action_type.value for a in plan.actions]
    assert types == EXPECTED_ORDER
    # Orders are strictly sequential 1..N.
    assert [a.order for a in plan.actions] == list(range(1, len(plan.actions) + 1))


def test_deployment_phase_precedence(plan):
    types = [a.action_type.value for a in plan.actions]
    canonical = [
        "verify_workspace", "create_connection", "create_lakehouse",
        "create_table", "create_dataflow", "create_pipeline",
        "configure_schedule", "run_target", "validate",
    ]
    first_index = {t: types.index(t) for t in canonical if t in types}
    ordered = [first_index[t] for t in canonical if t in first_index]
    assert ordered == sorted(ordered)


def test_required_change_becomes_conversion_action(plan):
    dataflow_action = next(
        a for a in plan.actions
        if a.action_type == MigrationActionType.CREATE_DATAFLOW
    )
    assert dataflow_action.requires_conversion is True
    assert dataflow_action.risk == MigrationRisk.MEDIUM


def test_clean_workload_is_executable(plan):
    assert plan.executable is True
    assert plan.overall_risk == MigrationRisk.MEDIUM
    assert plan.manual_actions == []
    assert plan.generated_package is not None
    assert plan.summary.generated_artifact_count == 8


# ── Validation rules ─────────────────────────────────────────────


def test_validation_rules_complete(plan):
    names = {r.name for r in plan.validation_rules}
    assert {
        "pipeline_run_status",
        "enriched_orders_row_count",
        "rejected_orders_row_count",
        "customer_summary_row_count",
        "output_schema",
        "total_gross_amount",
        "total_discount_amount",
        "total_net_amount",
        "customer_region_totals",
        "pipeline_runtime",
    } <= names
    assert len(plan.validation_rules) == 10


def test_validation_rule_tolerances(plan):
    by_name = {r.name: r for r in plan.validation_rules}
    # Money checks tolerate 0.01.
    for money in ("total_gross_amount", "total_discount_amount", "total_net_amount"):
        assert by_name[money].tolerance == 0.01
        assert by_name[money].blocking is True
    # Row counts must be exact.
    assert by_name["enriched_orders_row_count"].tolerance == 0.0


# ── Determinism ──────────────────────────────────────────────────


def test_plan_is_deterministic():
    inv = load_mock_adf_inventory(FIXTURES)
    disc = ADFDiscoveryService(inv).scan_inventory()
    assess = ADFCompatibilityAssessment(inv).assess_discovery(disc)
    first = MigrationPlanner(inv).generate_plan(disc, assess)
    second = MigrationPlanner(inv).generate_plan(disc, assess)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


# ── Synthetic edge cases ─────────────────────────────────────────


def _plan_for(inv):
    disc = ADFDiscoveryService(inv).scan_inventory()
    assess = ADFCompatibilityAssessment(inv).assess_discovery(disc)
    return MigrationPlanner(inv).generate_plan(disc, assess)


def test_blocked_assessment_makes_plan_non_executable():
    dataset = Dataset(
        name="ds_orphan",
        properties=DatasetProperties(
            type="Parquet",
            linkedServiceName=AssetReference(referenceName="ls_missing"),
        ),
    )
    plan = _plan_for(ADFInventory(datasets=[dataset]))
    assert plan.executable is False
    assert plan.overall_risk == MigrationRisk.CRITICAL


def test_unsupported_item_becomes_manual_action():
    pipeline = ADFPipeline(
        name="pl_x",
        properties=PipelineProperties(
            activities=[PipelineActivity(name="WeirdOne", type="QuantumActivity")]
        ),
    )
    plan = _plan_for(ADFInventory(pipelines=[pipeline]))
    manual_names = {m.source_asset for m in plan.manual_actions}
    assert "WeirdOne" in manual_names
    # The unsupported activity is explained, not mapped to a target.
    weird = next(m for m in plan.mappings if m.source_asset == "WeirdOne")
    assert weird.mapped is False
    assert weird.explanation


# ── Secrets ──────────────────────────────────────────────────────


def test_no_secrets_in_plan(plan):
    serialized = json.dumps(plan.model_dump(mode="json"))
    for token in (
        "password",
        "client_secret",
        "accountKey",
        "connectionString",
        "accessToken",
        "servicePrincipalKey",
    ):
        assert token not in serialized
