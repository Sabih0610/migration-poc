"""Tests for the Phase 4 compatibility assessment engine."""

import json
from pathlib import Path

import pytest

from src.fixtures_loader import load_mock_adf_inventory
from src.migration.assessment import ADFCompatibilityAssessment
from src.migration.discovery import ADFDiscoveryService
from src.models.schemas import (
    ADFInventory,
    AssessmentStatus,
    AssetReference,
    Dataset,
    DatasetProperties,
    LinkedService,
    LinkedServiceProperties,
    LinkedServiceTypeProperties,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def discovery():
    inv = load_mock_adf_inventory(FIXTURES)
    return inv, ADFDiscoveryService(inv).scan_inventory()


@pytest.fixture
def result(discovery):
    inv, disc = discovery
    return ADFCompatibilityAssessment(inv).assess_discovery(disc)


# ── Current workload ─────────────────────────────────────────────


def test_current_workload_assessed(result):
    # All 14 discovered assets are assessed.
    assert len(result.assessments) == 14
    assert result.summary.total_assets == 14
    # No hard blockers in the clean mock workload.
    assert result.summary.blocked_count == 0
    assert result.summary.unsupported_count == 0


def test_overall_status_requires_change(result):
    assert result.overall_status == AssessmentStatus.REQUIRES_CHANGE


def _issues_for(result, asset_name):
    return [
        i
        for a in result.assessments
        if a.asset_name == asset_name
        for i in a.issues
    ]


def _rule_ids(result):
    return {i.rule_id for a in result.assessments for i in a.issues}


def test_execute_dataflow_requires_change(result):
    exec_df = [
        a for a in result.assessments if a.asset_name == "ExecuteSalesDataFlow"
    ]
    assert exec_df, "ExecuteSalesDataFlow activity should be assessed"
    assert exec_df[0].status == AssessmentStatus.REQUIRES_CHANGE


def test_joins_split_aggregate_require_change(result):
    ids = _rule_ids(result)
    assert "DF-JOIN-001" in ids
    assert "DF-SPLIT-001" in ids
    assert "DF-AGGREGATE-001" in ids
    # And each of those issues carries REQUIRES_CHANGE.
    change_ids = {
        i.rule_id
        for a in result.assessments
        for i in a.issues
        if i.status == AssessmentStatus.REQUIRES_CHANGE
    }
    assert {"DF-JOIN-001", "DF-SPLIT-001", "DF-AGGREGATE-001"} <= change_ids


def test_schedule_trigger_needs_review(result):
    trg = [a for a in result.assessments if a.asset_type == "trigger"][0]
    assert trg.status == AssessmentStatus.NEEDS_REVIEW
    assert trg.issues[0].rule_id == "TRG-SCHEDULE-001"


def test_datasets_ready(result):
    datasets = [a for a in result.assessments if a.asset_type == "dataset"]
    assert len(datasets) == 6
    assert all(a.status == AssessmentStatus.READY for a in datasets)


# ── Synthetic edge cases ─────────────────────────────────────────


def test_unknown_activity_unsupported():
    from src.models.schemas import (
        ADFPipeline,
        PipelineActivity,
        PipelineProperties,
    )

    pipeline = ADFPipeline(
        name="pl_x",
        properties=PipelineProperties(
            activities=[PipelineActivity(name="WeirdOne", type="QuantumActivity")]
        ),
    )
    inv = ADFInventory(pipelines=[pipeline])
    disc = ADFDiscoveryService(inv).scan_inventory()
    res = ADFCompatibilityAssessment(inv).assess_discovery(disc)

    weird = [a for a in res.assessments if a.asset_name == "WeirdOne"][0]
    assert weird.status == AssessmentStatus.UNSUPPORTED
    assert res.overall_status == AssessmentStatus.UNSUPPORTED


def test_missing_linked_service_blocked():
    dataset = Dataset(
        name="ds_orphan",
        properties=DatasetProperties(
            type="DelimitedText",
            linkedServiceName=AssetReference(
                referenceName="ls_missing", type="LinkedServiceReference"
            ),
        ),
    )
    inv = ADFInventory(datasets=[dataset])  # no linked services -> missing
    disc = ADFDiscoveryService(inv).scan_inventory()
    res = ADFCompatibilityAssessment(inv).assess_discovery(disc)

    orphan = [a for a in res.assessments if a.asset_name == "ds_orphan"][0]
    assert orphan.status == AssessmentStatus.BLOCKED
    assert res.overall_status == AssessmentStatus.BLOCKED
    assert res.summary.blocking_issue_count >= 1


def test_embedded_credential_blocked():
    ls = LinkedService(
        name="ls_bad",
        properties=LinkedServiceProperties(
            type="AzureBlobFS",
            typeProperties=LinkedServiceTypeProperties(
                url="https://example.dfs.core.windows.net",
                accountKey="dummy-value",
            ),
        ),
    )
    inv = ADFInventory(linked_services=[ls])
    disc = ADFDiscoveryService(inv).scan_inventory()
    res = ADFCompatibilityAssessment(inv).assess_discovery(disc)

    bad = [a for a in res.assessments if a.asset_name == "ls_bad"][0]
    assert bad.status == AssessmentStatus.BLOCKED
    assert bad.issues[0].rule_id == "LS-EMBEDDED-CRED-001"


# ── Determinism & priority ───────────────────────────────────────


def test_status_priority_drives_overall():
    # A single BLOCKED asset must dominate everything else.
    dataset = Dataset(
        name="ds_orphan",
        properties=DatasetProperties(
            type="Parquet",
            linkedServiceName=AssetReference(referenceName="ls_missing"),
        ),
    )
    inv = ADFInventory(datasets=[dataset])
    disc = ADFDiscoveryService(inv).scan_inventory()
    res = ADFCompatibilityAssessment(inv).assess_discovery(disc)
    assert res.overall_status == AssessmentStatus.BLOCKED


def test_repeated_assessment_deterministic(discovery):
    inv, disc = discovery
    first = ADFCompatibilityAssessment(inv).assess_discovery(disc)
    second = ADFCompatibilityAssessment(inv).assess_discovery(disc)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


# ── Secrets ──────────────────────────────────────────────────────


def test_no_secrets_in_result(result):
    serialized = json.dumps(result.model_dump(mode="json"))
    for token in (
        "password",
        "client_secret",
        "accountKey",
        "connectionString",
        "accessToken",
        "servicePrincipalKey",
    ):
        assert token not in serialized
