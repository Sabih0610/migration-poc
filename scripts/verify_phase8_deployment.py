"""Verify package-aware approval and mock definition deployment."""

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_helper import TempDatabase
from src.api.app import app
from src.approvals.approval_store import get_approval
from src.connectors.mock_fabric_client import MockFabricClient
from src.migration.deployment import DeploymentService
from src.migration.plan_store import get_plan
from src.models.schemas import ApprovalStatus, DeploymentMode, DeploymentStatus


def main() -> int:
    print("=" * 60)
    print("  Phase 8 Package-aware Deployment Verification")
    print("=" * 60)
    with TempDatabase(prefix="verify_phase8_deployment_") as workspace:
        return _run(workspace)


def _run(workspace: TempDatabase) -> int:
    errors: list[str] = []
    passed: list[str] = []
    client = TestClient(app)

    try:
        client.post("/api/discovery/scan")
        client.post("/api/assessment/run")
        plan_response = client.post("/api/plans/generate")
        plan_id = plan_response.json()["plan_id"]
        request = client.post(
            f"/api/plans/{plan_id}/request-approval", json={"user": "alice"}
        )
        approval_id = request.json()["approval_id"]
        client.post(
            f"/api/approvals/{approval_id}/approve", json={"user": "bob"}
        )
        passed.append("Generated package approved")
    except Exception as exc:
        return _finish(passed, [f"Setup failed: {exc}"])

    dry = client.post(
        "/api/deployments/start",
        json={"plan_id": plan_id, "approval_id": approval_id, "mode": "DRY_RUN"},
    )
    if (
        dry.status_code == 200
        and dry.json()["status"] == "SUCCEEDED"
        and dry.json()["summary"]["resources_created"] == 0
        and len(dry.json()["steps"]) == 8
    ):
        passed.append("DRY_RUN validated 8 definitions and created nothing")
    else:
        errors.append(f"DRY_RUN failed: {dry.text}")

    mock = client.post(
        "/api/deployments/start",
        json={"plan_id": plan_id, "approval_id": approval_id, "mode": "MOCK"},
    )
    if (
        mock.status_code == 200
        and mock.json()["status"] == "SUCCEEDED"
        and mock.json()["summary"]["resources_created"] == 8
        and all(step["content_digest"] for step in mock.json()["steps"])
    ):
        passed.append("MOCK deployed 8 concrete definitions")
    else:
        errors.append(f"MOCK deployment failed: {mock.text}")

    connector = MockFabricClient()
    service = DeploymentService(connector)
    first = service.deploy(plan_id, approval_id, DeploymentMode.MOCK)
    second = service.deploy(plan_id, approval_id, DeploymentMode.MOCK)
    if (
        first.status == second.status == DeploymentStatus.SUCCEEDED
        and connector.resource_count() == 8
        and [step.resource_id for step in first.steps]
        == [step.resource_id for step in second.steps]
    ):
        passed.append("Repeated definition deployment is idempotent")
    else:
        errors.append("Repeated mock deployment was not idempotent")

    failing = DeploymentService(
        MockFabricClient(fail_on_action="create_table")
    ).deploy(plan_id, approval_id, DeploymentMode.MOCK)
    failed_steps = [step for step in failing.steps if step.status.value == "FAILED"]
    if (
        failing.status == DeploymentStatus.PARTIAL
        and len(failed_steps) == 1
        and all(
            step.status.value == "SKIPPED"
            for step in failing.steps if step.order > failed_steps[0].order
        )
    ):
        passed.append("Injected failure stopped later artifacts")
    else:
        errors.append("Injected failure did not stop safely")

    # Tampering with an approved artifact must invalidate authorization.
    record = get_plan(plan_id)
    manifest_path = workspace.generated_dir / record["package_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_path = workspace.generated_dir / manifest["entries"][0]["relative_path"]
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["warnings"] = ["tampered after approval"]
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    blocked = client.post(
        "/api/deployments/start",
        json={"plan_id": plan_id, "approval_id": approval_id, "mode": "MOCK"},
    )
    approval = get_approval(approval_id)
    if blocked.status_code == 409 and approval.status == ApprovalStatus.INVALIDATED:
        passed.append("Package tampering invalidated approval and blocked deployment")
    else:
        errors.append("Tampered approved package was not invalidated")

    return _finish(passed, errors)


def _finish(passed: list[str], errors: list[str]) -> int:
    for item in passed:
        print(f"  [OK] {item}")
    for item in errors:
        print(f"  [FAIL] {item}")
    print(f"  RESULT: {'FAIL' if errors else 'PASS'}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
