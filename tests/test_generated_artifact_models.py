"""Domain-model tests for the generated artifact foundation."""

import pytest
from pydantic import ValidationError

from src.models.schemas import (
    ArtifactManifest,
    ConversionDisposition,
    DefinitionSchemaResult,
    DeployableTargetType,
    GeneratedArtifact,
    GeneratedArtifactPackage,
    ManifestEntry,
    PropertyConversion,
)


def _artifact(target_type=DeployableTargetType.DATA_PIPELINE):
    return GeneratedArtifact(
        artifact_id="artifact:pipeline:orders",
        source_reference="pipeline:orders",
        target_type=target_type,
        target_name="orders",
        generated_definition={"name": "orders", "properties": {}},
        conversion_notes=[
            PropertyConversion(
                source_path="properties.parameters",
                target_path="properties.parameters",
                disposition=ConversionDisposition.PRESERVED,
            )
        ],
        warnings=[],
        unsupported_properties=[],
        manual_actions=[],
        dependencies=["artifact:connection:adls"],
        connection_references=["artifact:connection:adls"],
        content_digest="a" * 64,
    )


def test_generated_artifact_has_required_traceability_fields():
    artifact = _artifact()
    payload = artifact.model_dump(mode="json")
    for field in (
        "artifact_id",
        "source_reference",
        "target_type",
        "target_name",
        "generated_definition",
        "conversion_notes",
        "warnings",
        "unsupported_properties",
        "manual_actions",
        "dependencies",
        "connection_references",
        "content_digest",
    ):
        assert field in payload


def test_only_deployable_target_types_are_accepted():
    expected = {
        "FabricConnection",
        "Lakehouse",
        "LakehouseTable",
        "DataflowGen2",
        "FabricDataPipeline",
        "FabricSchedule",
    }
    assert {target.value for target in DeployableTargetType} == expected
    with pytest.raises(ValidationError):
        _artifact(target_type="Workspace")


def test_package_manifest_and_schema_result_round_trip():
    artifact = _artifact()
    entry = ManifestEntry(
        artifact_id=artifact.artifact_id,
        target_type=artifact.target_type,
        target_name=artifact.target_name,
        relative_path="pipelines/orders.json",
        content_digest=artifact.content_digest,
        dependencies=artifact.dependencies,
    )
    manifest = ArtifactManifest(package_id="package-1", entries=[entry])
    package = GeneratedArtifactPackage(
        package_id="package-1", artifacts=[artifact], manifest=manifest
    )
    restored = GeneratedArtifactPackage.model_validate_json(
        package.model_dump_json()
    )
    assert restored == package

    schema = DefinitionSchemaResult(valid=True, schema_name="pipeline-v1")
    assert schema.errors == []
