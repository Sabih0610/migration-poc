"""End-to-end verification for artifact structural validation."""

import json
import sys
from pathlib import Path
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_helper import TempDatabase
from src.api.app import app
from src.database import DeploymentRunRecord, ValidationRunRecord, get_session_factory

EXPECTED = {
    "source_to_target_mapping_coverage", "activity_coverage", "transformation_coverage_and_order",
    "parameter_preservation", "variable_preservation", "expression_conversion_or_preservation",
    "dependency_and_execution_order_preservation", "trigger_to_schedule_mapping",
    "connection_reference_mapping", "unsupported_property_reporting", "manual_action_reporting",
    "generated_definition_schema_validity", "manifest_digest_consistency",
    "deployed_definition_digest_consistency",
}


def main() -> int:
    print("Phase 8 Structural Verification")
    with TempDatabase(prefix="verify_phase8_structural_"):
        client = TestClient(app)
        errors = []
        discovery_id = client.post("/api/discovery/scan").json()["discovery_id"]
        client.post("/api/assessment/run")
        plan_id = client.post("/api/plans/generate").json()["plan_id"]
        approval_id = client.post(f"/api/plans/{plan_id}/request-approval", json={"user": "alice", "comment": "<script>x</script> password=private"}).json()["approval_id"]
        client.post(f"/api/approvals/{approval_id}/approve", json={"user": "bob"})
        deployment_id = client.post("/api/deployments/start", json={"plan_id": plan_id, "approval_id": approval_id, "mode": "MOCK"}).json()["deployment_id"]
        response = client.post("/api/validations/run", json={"deployment_id": deployment_id})
        result = response.json()
        if not (response.status_code == 200 and result.get("status") == "PASSED" and result.get("discovery_id") == discovery_id and {c["category"] for c in result.get("checks", [])} == EXPECTED):
            errors.append("structural comparison failed")
        else:
            print("  [OK] Snapshot/package/manifest/deployment comparison")
        report_json = client.get(f"/api/reports/{result.get('validation_id')}.json")
        report_html = client.get(f"/api/reports/{result.get('validation_id')}.html")
        if not (report_json.status_code == report_html.status_code == 200 and "private" not in report_json.text and "<script>" not in report_html.text and "generated_artifacts" in report_json.text):
            errors.append("safe artifact reports failed")
        else:
            print("  [OK] Redacted and escaped artifact reports")
        session = get_session_factory()()
        try:
            if session.query(ValidationRunRecord).count() != 0:
                errors.append("legacy validation_runs table was used")
            record = session.get(DeploymentRunRecord, deployment_id)
            payload = json.loads(record.result_json)
            payload["steps"][0]["content_digest"] = "bad"
            record.result_json = json.dumps(payload)
            session.commit()
        finally:
            session.close()
        if client.post("/api/validations/run", json={"deployment_id": deployment_id}).json().get("status") != "FAILED":
            errors.append("deployed digest tampering was not detected")
        else:
            print("  [OK] Deployed digest tampering detected")
        for error in errors:
            print(f"  [FAIL] {error}")
        print(f"  RESULT: {'FAIL' if errors else 'PASS'}")
        return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
