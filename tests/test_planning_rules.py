"""Tests for the Phase 5 source-to-target mapping rules."""

from src.migration import planning_rules as pr
from src.models.schemas import (
    AssessmentStatus,
    MigrationActionType,
    TargetItemType,
)


def test_linked_service_maps_to_connection():
    rule = pr.map_linked_service()
    assert rule.target_item_type == TargetItemType.CONNECTION
    assert rule.action_type == MigrationActionType.CREATE_CONNECTION
    assert rule.rule_id == "MAP-LS-ADLS-001"


def test_source_dataset_maps_into_dataflow():
    rule = pr.map_source_dataset()
    assert rule.target_item_type == TargetItemType.DATAFLOW_GEN2
    # Source datasets are absorbed into the dataflow -> no standalone action.
    assert rule.action_type is None


def test_parquet_sink_maps_to_lakehouse_table():
    rule = pr.map_sink_dataset(rejected=False)
    assert rule.target_item_type == TargetItemType.LAKEHOUSE_TABLE
    assert rule.action_type == MigrationActionType.CREATE_TABLE
    assert rule.rule_id == "MAP-DS-PARQUET-SINK-001"


def test_rejected_sink_maps_to_rejected_table():
    rule = pr.map_sink_dataset(rejected=True)
    assert rule.target_item_type == TargetItemType.LAKEHOUSE_TABLE
    assert rule.rule_id == "MAP-DS-CSV-SINK-001"


def test_data_flow_maps_to_dataflow_gen2():
    rule = pr.map_data_flow()
    assert rule.target_item_type == TargetItemType.DATAFLOW_GEN2
    assert rule.action_type == MigrationActionType.CREATE_DATAFLOW


def test_pipeline_maps_to_data_pipeline():
    rule = pr.map_pipeline()
    assert rule.target_item_type == TargetItemType.DATA_PIPELINE
    assert rule.action_type == MigrationActionType.CREATE_PIPELINE


def test_schedule_trigger_maps_to_schedule():
    rule = pr.map_trigger("ScheduleTrigger")
    assert rule.target_item_type == TargetItemType.SCHEDULE
    assert rule.action_type == MigrationActionType.CONFIGURE_SCHEDULE


def test_unknown_trigger_has_no_target():
    rule = pr.map_trigger("TumblingWindowTrigger")
    assert rule.target_item_type == TargetItemType.NONE
    assert rule.action_type is None


def test_action_kind_by_status():
    assert pr.action_kind(AssessmentStatus.READY) == pr.AUTOMATIC
    assert pr.action_kind(AssessmentStatus.NEEDS_REVIEW) == pr.WARNING
    assert pr.action_kind(AssessmentStatus.REQUIRES_CHANGE) == pr.CONVERSION
    assert pr.action_kind(AssessmentStatus.UNSUPPORTED) == pr.MANUAL
    assert pr.action_kind(AssessmentStatus.BLOCKED) == pr.BLOCKED
