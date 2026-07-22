"""Phase 8 structural validation and artifact-report integration tests."""

import json

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.database import (
    DeploymentRunRecord,
    RuntimeValidationRunRecord,
    StructuralValidationRunRecord,
    ValidationRunRecord,
    get_session_factory,
)
from src.models.schemas import DatasetMetrics
from src.validation.mock_results import MockResultProvider
from src.reports.report_service import redact_secrets


@pytest.fixture()
def migrated():
    client = TestClient(app)
    discovery = client.post("/api/discovery/scan").json()
    assert client.post("/api/assessment/run").status_code == 200
    plan = client.post("/api/plans/generate").json()
    approval = client.post(
        f"/api/plans/{plan['plan_id']}/request-approval",
        json={"user": "alice", "comment": "<script>alert(1)</script> password=hunter2"},
    ).json()
    assert client.post(
        f"/api/approvals/{approval['approval_id']}/approve",
        json={"user": "bob"},
    ).status_code == 200
    dry = client.post("/api/deployments/start", json={
        "plan_id": plan["plan_id"], "approval_id": approval["approval_id"],
        "mode": "DRY_RUN",
    }).json()
    mock = client.post("/api/deployments/start", json={
        "plan_id": plan["plan_id"], "approval_id": approval["approval_id"],
        "mode": "MOCK",
    }).json()
    assert mock["status"] == "SUCCEEDED"
    return {
        "client": client, "discovery_id": discovery["discovery_id"],
        "plan_id": plan["plan_id"], "approval_id": approval["approval_id"],
        "dry_id": dry["deployment_id"], "deployment_id": mock["deployment_id"],
    }


EXPECTED_CATEGORIES = {
    "source_to_target_mapping_coverage", "activity_coverage",
    "transformation_coverage_and_order", "parameter_preservation",
    "variable_preservation", "expression_conversion_or_preservation",
    "dependency_and_execution_order_preservation", "trigger_to_schedule_mapping",
    "connection_reference_mapping", "unsupported_property_reporting",
    "manual_action_reporting", "generated_definition_schema_validity",
    "manifest_digest_consistency", "deployed_definition_digest_consistency",
}


def test_structural_validation_passes_all_categories(migrated):
    response = migrated["client"].post(
        "/api/validations/run", json={"deployment_id": migrated["deployment_id"]}
    )
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "PASSED"
    assert result["discovery_id"] == migrated["discovery_id"]
    assert {check["category"] for check in result["checks"]} == EXPECTED_CATEGORIES
    assert result["summary"]["failed"] == 0

    session = get_session_factory()()
    try:
        assert session.query(StructuralValidationRunRecord).count() == 1
        assert session.query(ValidationRunRecord).count() == 0
    finally:
        session.close()


def test_validation_uses_plan_snapshot_not_newest_discovery(migrated):
    migrated["client"].post("/api/discovery/scan")
    result = migrated["client"].post(
        "/api/validations/run", json={"deployment_id": migrated["deployment_id"]}
    ).json()
    assert result["discovery_id"] == migrated["discovery_id"]


def test_only_successful_mock_deployment_can_validate(migrated):
    response = migrated["client"].post(
        "/api/validations/run", json={"deployment_id": migrated["dry_id"]}
    )
    assert response.status_code == 409
    assert "MOCK" in response.json()["detail"]
    assert migrated["client"].post(
        "/api/validations/run", json={"deployment_id": 999999}
    ).status_code == 409


def test_deployed_definition_tampering_is_structural_failure(migrated):
    session = get_session_factory()()
    try:
        record = session.get(DeploymentRunRecord, migrated["deployment_id"])
        payload = json.loads(record.result_json)
        payload["steps"][0]["content_digest"] = "0" * 64
        record.result_json = json.dumps(payload)
        session.commit()
    finally:
        session.close()
    result = migrated["client"].post(
        "/api/validations/run", json={"deployment_id": migrated["deployment_id"]}
    ).json()
    assert result["status"] == "FAILED"
    check = next(c for c in result["checks"] if c["category"] == "deployed_definition_digest_consistency")
    assert check["status"] == "FAILED"


def test_missing_deployed_activity_fails_component_coverage(migrated):
    session = get_session_factory()()
    try:
        record = session.get(DeploymentRunRecord, migrated["deployment_id"])
        payload = json.loads(record.result_json)
        pipeline = next(step for step in payload["steps"]
                        if step["target_item_type"] == "FabricDataPipeline")
        pipeline["generated_definition"]["properties"]["activities"].pop()
        record.result_json = json.dumps(payload)
        session.commit()
    finally:
        session.close()
    result = migrated["client"].post(
        "/api/validations/run", json={"deployment_id": migrated["deployment_id"]}
    ).json()
    check = next(c for c in result["checks"] if c["category"] == "activity_coverage")
    assert result["status"] == "FAILED"
    assert check["status"] == "FAILED"
    assert check["details"]["missing_in_deployment"]


def test_modified_deployed_transformation_order_fails_coverage(migrated):
    session = get_session_factory()()
    try:
        record = session.get(DeploymentRunRecord, migrated["deployment_id"])
        payload = json.loads(record.result_json)
        dataflow = next(step for step in payload["steps"]
                        if step["target_item_type"] == "DataflowGen2")
        transforms = dataflow["generated_definition"]["properties"]["transformations"]
        transforms.reverse()
        record.result_json = json.dumps(payload)
        session.commit()
    finally:
        session.close()
    result = migrated["client"].post(
        "/api/validations/run", json={"deployment_id": migrated["deployment_id"]}
    ).json()
    check = next(c for c in result["checks"]
                 if c["category"] == "transformation_coverage_and_order")
    assert result["status"] == "FAILED"
    assert check["status"] == "FAILED"


def test_runtime_metrics_are_optional_and_separate(migrated, monkeypatch):
    original = MockResultProvider.get_target_metrics

    def mismatched(self):
        metrics = original(self)
        metrics["enriched_orders"] = DatasetMetrics(row_count=1, schema_hash="wrong")
        return metrics

    monkeypatch.setattr(MockResultProvider, "get_target_metrics", mismatched)
    response = migrated["client"].post(
        "/api/runtime-validations/run", json={"deployment_id": migrated["deployment_id"]}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "FAILED"
    session = get_session_factory()()
    try:
        assert session.query(RuntimeValidationRunRecord).count() == 1
        assert session.query(StructuralValidationRunRecord).count() == 0
        assert session.query(ValidationRunRecord).count() == 0
    finally:
        session.close()


def test_artifact_reports_are_redacted_escaped_and_structural(migrated):
    result = migrated["client"].post(
        "/api/validations/run", json={"deployment_id": migrated["deployment_id"]}
    ).json()
    validation_id = result["validation_id"]
    json_response = migrated["client"].get(f"/api/reports/{validation_id}.json")
    html_response = migrated["client"].get(f"/api/reports/{validation_id}.html")
    assert json_response.status_code == html_response.status_code == 200
    report = json_response.json()
    assert report["structural_validation"]["status"] == "PASSED"
    assert set(report["workflow_stages"]) == {
        "discover", "assess", "plan", "approve", "deploy", "validate"
    }
    for field in ("source_artifacts", "generated_artifacts", "mappings",
                  "property_conversions", "manual_actions", "approval", "deployment"):
        assert field in report
    assert report["generated_artifacts"][0]["generated_definition"]
    assert "hunter2" not in json_response.text
    assert "***REDACTED***" in json_response.text
    assert "<script>" not in html_response.text
    assert "&lt;script&gt;" in html_response.text
    assert "<pre>" not in html_response.text


def test_recursive_report_redaction_handles_nested_lists_and_values():
    redacted = redact_secrets({
        "outer": [{"client_secret": "alpha"}, {"safe": "token=beta"}],
        "authorization": "Bearer gamma",
    })
    serialized = json.dumps(redacted)
    assert all(secret not in serialized for secret in ("alpha", "beta", "gamma"))
    assert serialized.count("***REDACTED***") == 3


def test_validation_page_is_structural_and_avoids_raw_html_injection(migrated):
    page = migrated["client"].get("/validation")
    script = migrated["client"].get("/validation.js")
    assert "Artifact Structural Validation" in page.text
    assert "Customer rows" in page.text
    assert "innerHTML" not in script.text
    assert "runtime-validations" in script.text


def test_report_routes_reject_non_numeric_traversal(migrated):
    response = migrated["client"].get("/api/reports/..%2Fsecrets.json")
    assert response.status_code in {404, 422}
