"""Tests for the Phase 4 assessment rules."""

from src.migration import assessment_rules as rules
from src.models.schemas import (
    AssessmentStatus,
    status_priority,
    worst_status,
)

READY = AssessmentStatus.READY
NEEDS_REVIEW = AssessmentStatus.NEEDS_REVIEW
REQUIRES_CHANGE = AssessmentStatus.REQUIRES_CHANGE
UNSUPPORTED = AssessmentStatus.UNSUPPORTED
BLOCKED = AssessmentStatus.BLOCKED


# ── Activities ───────────────────────────────────────────────────


def test_known_activities_ready():
    assert rules.assess_activity("GetMetadata").status == READY
    assert rules.assess_activity("IfCondition").status == READY
    assert rules.assess_activity("Fail").status == READY


def test_execute_dataflow_requires_change():
    outcome = rules.assess_activity("ExecuteDataFlow")
    assert outcome.status == REQUIRES_CHANGE
    assert outcome.rule_id == "ACT-EXECUTEDATAFLOW-001"


def test_unknown_activity_unsupported():
    outcome = rules.assess_activity("SomeFutureActivity")
    assert outcome.status == UNSUPPORTED
    assert outcome.rule_id == "ACT-UNKNOWN-001"


# ── Transformations ──────────────────────────────────────────────


def test_ready_transformations():
    assert rules.classify_transformation("filter", "f").status == READY
    assert rules.classify_transformation("derive", "d").status == READY


def test_join_requires_change():
    assert rules.classify_transformation("join", "j").status == REQUIRES_CHANGE


def test_split_requires_change():
    assert rules.classify_transformation("split", "s").status == REQUIRES_CHANGE


def test_aggregate_requires_change():
    assert rules.classify_transformation("aggregate", "a").status == REQUIRES_CHANGE


def test_unknown_transformation_unsupported():
    assert rules.classify_transformation("pivot", "p").status == UNSUPPORTED
    assert rules.classify_transformation(None, "x").status == UNSUPPORTED


def test_multiple_sinks_needs_review():
    assert rules.assess_sink_count(1) is None
    outcome = rules.assess_sink_count(3)
    assert outcome is not None
    assert outcome.status == NEEDS_REVIEW


# ── Datasets ─────────────────────────────────────────────────────


def test_csv_and_parquet_ready():
    assert rules.assess_dataset("DelimitedText", False).status == READY
    assert rules.assess_dataset("Parquet", False).status == READY


def test_missing_linked_service_blocked():
    outcome = rules.assess_dataset("DelimitedText", True)
    assert outcome.status == BLOCKED
    assert outcome.rule_id == "DS-MISSING-LS-001"


def test_unknown_dataset_needs_review():
    assert rules.assess_dataset("CosmosDb", False).status == NEEDS_REVIEW


# ── Linked services ──────────────────────────────────────────────


def test_adls_url_only_needs_review():
    outcome = rules.assess_linked_service("AzureBlobFS", False)
    assert outcome.status == NEEDS_REVIEW


def test_embedded_credential_blocked():
    outcome = rules.assess_linked_service("AzureBlobFS", True)
    assert outcome.status == BLOCKED
    assert outcome.rule_id == "LS-EMBEDDED-CRED-001"


# ── Triggers ─────────────────────────────────────────────────────


def test_schedule_trigger_needs_review():
    outcome = rules.assess_trigger("ScheduleTrigger")
    assert outcome.status == NEEDS_REVIEW


def test_unknown_trigger_unsupported():
    assert rules.assess_trigger("TumblingWindowTrigger").status == UNSUPPORTED


# ── Status priority ──────────────────────────────────────────────


def test_status_priority_order():
    assert status_priority(BLOCKED) > status_priority(UNSUPPORTED)
    assert status_priority(UNSUPPORTED) > status_priority(REQUIRES_CHANGE)
    assert status_priority(REQUIRES_CHANGE) > status_priority(NEEDS_REVIEW)
    assert status_priority(NEEDS_REVIEW) > status_priority(READY)


def test_worst_status():
    assert worst_status([READY, NEEDS_REVIEW, REQUIRES_CHANGE]) == REQUIRES_CHANGE
    assert worst_status([READY, BLOCKED, UNSUPPORTED]) == BLOCKED
    assert worst_status([UNSUPPORTED, REQUIRES_CHANGE]) == UNSUPPORTED
    assert worst_status([]) == READY
