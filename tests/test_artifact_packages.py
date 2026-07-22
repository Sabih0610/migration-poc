"""Generated Fabric definition and package security tests."""

import json
from pathlib import Path

import pytest

from src.artifacts import (
    ArtifactPackageError,
    build_package,
    read_package,
    safe_filename,
    validate_generated_artifact,
    write_package,
)
from src.fixtures_loader import load_mock_adf_inventory
from src.migration.assessment import ADFCompatibilityAssessment
from src.migration.discovery import ADFDiscoveryService
from src.migration.plan_store import compute_plan_fingerprint
from src.migration.planner import MigrationPlanner
from src.models.schemas import (
    ConversionDisposition,
    DeployableTargetType,
    MigrationPlan,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def plan():
    inventory = load_mock_adf_inventory(FIXTURES)
    discovery = ADFDiscoveryService(inventory).scan_inventory()
    assessment = ADFCompatibilityAssessment(inventory).assess_discovery(discovery)
    return MigrationPlanner(inventory).generate_plan(discovery, assessment)


def test_concrete_artifact_set(plan):
    package = plan.generated_package
    assert package is not None
    assert len(package.artifacts) == 8
    counts = {}
    for artifact in package.artifacts:
        counts[artifact.target_type] = counts.get(artifact.target_type, 0) + 1
        assert artifact.generated_definition["type"] == artifact.target_type.value
        assert artifact.generated_definition["name"] == artifact.target_name
        assert len(artifact.content_digest) == 64
        assert validate_generated_artifact(artifact).valid
    assert counts == {
        DeployableTargetType.CONNECTION: 1,
        DeployableTargetType.LAKEHOUSE: 1,
        DeployableTargetType.LAKEHOUSE_TABLE: 3,
        DeployableTargetType.DATAFLOW_GEN2: 1,
        DeployableTargetType.DATA_PIPELINE: 1,
        DeployableTargetType.SCHEDULE: 1,
    }


def test_nested_pipeline_and_dataflow_components(plan):
    artifacts = {artifact.target_type: artifact for artifact in plan.generated_package.artifacts}
    pipeline = artifacts[DeployableTargetType.DATA_PIPELINE].generated_definition
    dataflow = artifacts[DeployableTargetType.DATAFLOW_GEN2].generated_definition
    assert pipeline["properties"]["parameters"]["RunDate"]["type"] == "String"
    assert pipeline["properties"]["variables"]["ProcessingStatus"]["defaultValue"] == "pending"
    activities = pipeline["properties"]["activities"]
    condition = next(item for item in activities if item["type"] == "IfCondition")
    assert condition["properties"]["ifTrueActivities"][0]["type"] == "InvokeDataflowGen2"
    assert len(dataflow["properties"]["transformations"]) == 7


def test_conversion_dispositions_are_constrained(plan):
    allowed = {
        "PRESERVED", "RENAMED", "CONVERTED", "UNSUPPORTED", "MANUAL",
        "OMITTED_WITH_REASON",
    }
    assert {item.value for item in ConversionDisposition} == allowed
    used = {
        conversion.disposition.value
        for artifact in plan.generated_package.artifacts
        for conversion in artifact.conversion_notes
    }
    assert {"PRESERVED", "RENAMED", "CONVERTED", "MANUAL",
            "OMITTED_WITH_REASON"} <= used


def test_package_write_read_round_trip(plan, tmp_path):
    package = plan.generated_package
    manifest_path = write_package(package, tmp_path)
    expected_dirs = {
        "connections", "lakehouses", "dataflows", "pipelines", "schedules",
        "manifests",
    }
    assert expected_dirs <= {path.name for path in tmp_path.iterdir() if path.is_dir()}
    restored = read_package(tmp_path, manifest_path.relative_to(tmp_path))
    assert restored.model_dump(mode="json") == package.model_dump(mode="json")
    assert not list(tmp_path.rglob(".tmp-*"))


def test_package_output_is_deterministic(plan, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    path1 = write_package(plan.generated_package, first)
    path2 = write_package(plan.generated_package, second)
    assert path1.read_bytes() == path2.read_bytes()
    files1 = {
        path.relative_to(first).as_posix(): path.read_bytes()
        for path in first.rglob("*.json")
    }
    files2 = {
        path.relative_to(second).as_posix(): path.read_bytes()
        for path in second.rglob("*.json")
    }
    assert files1 == files2


def test_path_traversal_is_rejected(plan, tmp_path):
    write_package(plan.generated_package, tmp_path)
    assert ".." not in safe_filename("../../outside")
    with pytest.raises(ArtifactPackageError, match="escapes"):
        read_package(tmp_path, "../outside.json")


def test_tampered_artifact_fails_digest_check(plan, tmp_path):
    manifest_path = write_package(plan.generated_package, tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_path = tmp_path / manifest["entries"][0]["relative_path"]
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    payload["generated_definition"]["name"] = "tampered"
    artifact_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactPackageError, match="digest mismatch"):
        read_package(tmp_path, manifest_path.relative_to(tmp_path))


def test_invalid_definition_schema_is_rejected(plan, tmp_path):
    invalid = plan.generated_package.artifacts[0].model_copy(deep=True)
    invalid.generated_definition.pop("properties")
    package = build_package([invalid])
    result = validate_generated_artifact(package.artifacts[0])
    assert result.valid is False
    with pytest.raises(ArtifactPackageError, match="invalid generated definition"):
        write_package(package, tmp_path)


def test_fingerprint_excludes_persistence_and_location_metadata(plan):
    baseline = compute_plan_fingerprint(plan)
    changed_metadata = plan.model_copy(deep=True)
    changed_metadata.generated_package.manifest.plan_id = 999
    changed_metadata.generated_package.manifest.plan_version = 42
    changed_metadata.generated_package.output_directory = "C:/absolute/private/path"
    assert compute_plan_fingerprint(changed_metadata) == baseline

    payload = plan.model_dump(mode="json")
    payload.update({"plan_id": 123, "created_at": "2099-01-01T00:00:00Z"})
    assert compute_plan_fingerprint(MigrationPlan(**payload)) == baseline

    changed_content = plan.model_copy(deep=True)
    changed_content.generated_package.artifacts[0].warnings.append("content changed")
    assert compute_plan_fingerprint(changed_content) != baseline


def test_fingerprint_covers_all_approved_package_content(plan):
    baseline = compute_plan_fingerprint(plan)

    mutations = []
    generated = plan.model_copy(deep=True)
    generated.generated_package.artifacts[0].generated_definition["properties"][
        "endpoint"
    ] = "https://changed.invalid"
    mutations.append(generated)

    manifest = plan.model_copy(deep=True)
    manifest.generated_package.manifest.entries[0].relative_path = "changed.json"
    mutations.append(manifest)

    digest = plan.model_copy(deep=True)
    digest.generated_package.artifacts[0].content_digest = "0" * 64
    mutations.append(digest)

    warnings = plan.model_copy(deep=True)
    warnings.generated_package.artifacts[0].warnings.append("new warning")
    mutations.append(warnings)

    conversions = plan.model_copy(deep=True)
    conversions.generated_package.artifacts[0].conversion_notes[0].note += " changed"
    mutations.append(conversions)

    unsupported = plan.model_copy(deep=True)
    unsupported.generated_package.artifacts[0].unsupported_properties.append(
        "properties.unsupported"
    )
    mutations.append(unsupported)

    manual = plan.model_copy(deep=True)
    manual.generated_package.artifacts[0].manual_actions.append("manual change")
    mutations.append(manual)

    assert all(compute_plan_fingerprint(item) != baseline for item in mutations)
