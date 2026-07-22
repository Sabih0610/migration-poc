"""Definition-aware deployment service tests."""

from pathlib import Path

import pytest

from src.approvals import approval_service
from src.connectors.mock_fabric_client import MockFabricClient
from src.fixtures_loader import load_mock_adf_inventory
from src.migration.assessment import ADFCompatibilityAssessment
from src.migration.deployment import (
    DeploymentService,
    FabricDeploymentDisabledError,
)
from src.migration.discovery import ADFDiscoveryService
from src.migration.plan_store import save_plan
from src.migration.planner import MigrationPlanner
from src.models.schemas import (
    DeploymentMode,
    DeploymentStatus,
    DeploymentStepStatus,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def approved_plan():
    inventory = load_mock_adf_inventory(FIXTURES)
    discovery = ADFDiscoveryService(inventory).scan_inventory()
    assessment = ADFCompatibilityAssessment(inventory).assess_discovery(discovery)
    plan = MigrationPlanner(inventory).generate_plan(discovery, assessment)
    record = save_plan(plan, assessment_id=1)
    approval = approval_service.request_approval(record["id"], "alice")
    approval_service.approve(approval.approval_id, "bob")
    return record, approval


def test_dry_run_creates_nothing(approved_plan):
    record, approval = approved_plan
    connector = MockFabricClient()
    result = DeploymentService(connector).deploy(
        record["id"], approval.approval_id, DeploymentMode.DRY_RUN
    )
    assert result.status == DeploymentStatus.SUCCEEDED
    assert len(result.steps) == 8
    assert result.summary.resources_created == 0
    assert connector.resource_count() == 0
    assert all(step.generated_definition for step in result.steps)


def test_mock_deploys_definitions_in_dependency_order(approved_plan):
    record, approval = approved_plan
    connector = MockFabricClient()
    result = DeploymentService(connector).deploy(
        record["id"], approval.approval_id, DeploymentMode.MOCK
    )
    assert result.status == DeploymentStatus.SUCCEEDED
    assert connector.resource_count() == 8
    positions = {step.artifact_id: step.order for step in result.steps}
    artifacts = record["plan"].generated_package.artifacts
    for artifact in artifacts:
        assert all(positions[item] < positions[artifact.artifact_id]
                   for item in artifact.dependencies)
        deployed = connector.get_deployed_artifact(artifact.artifact_id)
        assert deployed["content_digest"] == artifact.content_digest
        assert deployed["generated_definition"] == artifact.generated_definition


def test_repeat_mock_deployment_is_idempotent(approved_plan):
    record, approval = approved_plan
    connector = MockFabricClient()
    service = DeploymentService(connector)
    first = service.deploy(record["id"], approval.approval_id, DeploymentMode.MOCK)
    count = connector.resource_count()
    second = service.deploy(record["id"], approval.approval_id, DeploymentMode.MOCK)
    assert first.status == second.status == DeploymentStatus.SUCCEEDED
    assert connector.resource_count() == count == 8
    assert [step.resource_id for step in first.steps] == [
        step.resource_id for step in second.steps
    ]


def test_injected_failure_stops_later_artifacts(approved_plan):
    record, approval = approved_plan
    connector = MockFabricClient(fail_on_action="create_table")
    result = DeploymentService(connector).deploy(
        record["id"], approval.approval_id, DeploymentMode.MOCK
    )
    failed = next(
        step for step in result.steps
        if step.status == DeploymentStepStatus.FAILED
    )
    assert result.status == DeploymentStatus.PARTIAL
    assert failed.target_item_type == "LakehouseTable"
    assert all(
        step.status == DeploymentStepStatus.SKIPPED
        for step in result.steps if step.order > failed.order
    )


def test_real_mode_disabled_by_default(approved_plan):
    # REAL is implemented in Phase 10 but stays disabled unless explicitly
    # enabled + configured; requesting it otherwise is a hard, safe stop.
    record, approval = approved_plan
    with pytest.raises(FabricDeploymentDisabledError):
        DeploymentService().deploy(
            record["id"], approval.approval_id, DeploymentMode.REAL
        )
