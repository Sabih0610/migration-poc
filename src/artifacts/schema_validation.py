"""Internal schema checks for generated Fabric definition JSON.

These schemas describe this PoC's deterministic interchange format. They do
not call or claim to reproduce a live Fabric API schema.
"""

from src.models.schemas import (
    DefinitionSchemaResult,
    DeployableTargetType,
    GeneratedArtifact,
)


_REQUIRED_PROPERTIES = {
    DeployableTargetType.CONNECTION: {"connectionType", "endpoint", "authentication"},
    DeployableTargetType.LAKEHOUSE: {"description"},
    DeployableTargetType.LAKEHOUSE_TABLE: {"lakehouse", "format", "schema"},
    DeployableTargetType.DATAFLOW_GEN2: {
        "sources", "transformations", "sinks", "connectionReferences"
    },
    DeployableTargetType.DATA_PIPELINE: {
        "parameters", "variables", "activities"
    },
    DeployableTargetType.SCHEDULE: {"recurrence", "pipeline", "parameters"},
}


def validate_generated_artifact(
    artifact: GeneratedArtifact,
) -> DefinitionSchemaResult:
    """Validate one generated definition against the internal type schema."""
    definition = artifact.generated_definition
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(definition, dict):
        errors.append("generated_definition must be an object")
        definition = {}
    if definition.get("type") != artifact.target_type.value:
        errors.append(
            "definition type must equal target_type "
            f"'{artifact.target_type.value}'"
        )
    if definition.get("name") != artifact.target_name:
        errors.append("definition name must equal target_name")

    properties = definition.get("properties")
    if not isinstance(properties, dict):
        errors.append("definition properties must be an object")
        properties = {}
    required = _REQUIRED_PROPERTIES[artifact.target_type]
    missing = sorted(required - set(properties))
    if missing:
        errors.append(f"missing required properties: {', '.join(missing)}")

    if artifact.target_type == DeployableTargetType.DATA_PIPELINE:
        if not isinstance(properties.get("activities", []), list):
            errors.append("pipeline activities must be an array")
    if artifact.target_type == DeployableTargetType.DATAFLOW_GEN2:
        for field in ("sources", "transformations", "sinks"):
            if not isinstance(properties.get(field, []), list):
                errors.append(f"dataflow {field} must be an array")
    if artifact.unsupported_properties:
        warnings.append("artifact reports unsupported source properties")

    return DefinitionSchemaResult(
        valid=not errors,
        schema_name=f"migration-poc/{artifact.target_type.value}/v1",
        errors=errors,
        warnings=warnings,
    )
