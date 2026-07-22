"""Verify deterministic concrete Fabric artifact package generation."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_helper import TempDatabase
from src.api.app import app
from src.artifacts import read_package, validate_generated_artifact
from src.migration.plan_store import compute_plan_fingerprint, get_latest_plan
from src.models.schemas import DeployableTargetType


def main() -> int:
    print("=" * 60)
    print("  Phase 8 Artifact Package Verification")
    print("=" * 60)
    with TempDatabase(prefix="verify_phase8_packages_") as workspace:
        return _run(workspace)


def _run(workspace: TempDatabase) -> int:
    errors: list[str] = []
    client = TestClient(app)
    for endpoint in (
        "/api/discovery/scan",
        "/api/assessment/run",
        "/api/plans/generate",
    ):
        response = client.post(endpoint)
        if response.status_code != 200:
            errors.append(f"{endpoint} failed: {response.status_code} {response.text}")
            return _finish(errors)

    record = get_latest_plan()
    package = record["plan"].generated_package
    if package is None:
        errors.append("Plan did not contain a generated artifact package")
        return _finish(errors)

    manifest_path = record["package_manifest_path"]
    try:
        restored = read_package(workspace.generated_dir, manifest_path)
    except Exception as exc:
        errors.append(f"Package read/digest validation failed: {exc}")
        return _finish(errors)

    if restored.model_dump(mode="json") != package.model_dump(mode="json"):
        errors.append("Written package did not round-trip deterministically")
    if len(package.artifacts) != 8:
        errors.append(f"Expected 8 generated artifacts, got {len(package.artifacts)}")
    for artifact in package.artifacts:
        schema = validate_generated_artifact(artifact)
        if not schema.valid:
            errors.append(
                f"Schema failed for {artifact.artifact_id}: {schema.errors}"
            )

    expected_types = {
        DeployableTargetType.CONNECTION,
        DeployableTargetType.LAKEHOUSE,
        DeployableTargetType.LAKEHOUSE_TABLE,
        DeployableTargetType.DATAFLOW_GEN2,
        DeployableTargetType.DATA_PIPELINE,
        DeployableTargetType.SCHEDULE,
    }
    actual_types = {artifact.target_type for artifact in package.artifacts}
    if actual_types != expected_types:
        errors.append(f"Unexpected artifact types: {sorted(actual_types)}")

    fingerprint = compute_plan_fingerprint(record["plan"])
    if len(fingerprint) != 64:
        errors.append("Plan fingerprint is not SHA-256")

    print(f"  Package:   {package.package_id}")
    print(f"  Artifacts: {len(package.artifacts)}")
    for artifact in package.artifacts:
        print(
            f"    {artifact.target_type.value:<20} "
            f"{artifact.target_name} {artifact.content_digest[:12]}"
        )
    return _finish(errors)


def _finish(errors: list[str]) -> int:
    if errors:
        for error in errors:
            print(f"  [FAIL] {error}")
        print("  RESULT: FAIL")
        return 1
    print("  RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
