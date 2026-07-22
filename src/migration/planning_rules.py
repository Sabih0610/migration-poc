"""Phase 5 source-to-target mapping rules (ADF → Microsoft Fabric).

Pure, deterministic mapping functions with stable rule IDs. Each
function returns a MappingRule describing the Fabric target for a
source asset category and, where applicable, the deployment action
that creates it. No orchestration, ordering, or I/O here.
"""

from typing import Any, NamedTuple, Optional

from src.models.schemas import (
    AssessmentStatus,
    ADFPipeline,
    ConversionDisposition,
    Dataset,
    LinkedService,
    MappingDataFlow,
    MigrationActionType,
    PropertyConversion,
    TargetItemType,
    Trigger,
)


class MappingRule(NamedTuple):
    """A source-category → Fabric-target mapping decision."""

    rule_id: str
    target_item_type: TargetItemType
    # The deployment action that creates this target, or None when the
    # asset is absorbed into another item (e.g. a source dataset becomes
    # configuration inside a Dataflow Gen2).
    action_type: Optional[MigrationActionType]
    explanation: str


class DefinitionSpec(NamedTuple):
    """Concrete target definition plus source-property traceability."""

    definition: dict[str, Any]
    conversions: list[PropertyConversion]
    warnings: list[str]
    unsupported_properties: list[str]
    manual_actions: list[str]


# ── Asset-category mappings ──────────────────────────────────────


def map_linked_service() -> MappingRule:
    """ADLS linked service → Fabric connection."""
    return MappingRule(
        "MAP-LS-ADLS-001",
        TargetItemType.CONNECTION,
        MigrationActionType.CREATE_CONNECTION,
        "ADLS linked service becomes a Fabric connection (managed identity).",
    )


def map_source_dataset() -> MappingRule:
    """CSV source dataset → Dataflow Gen2 source (configuration)."""
    return MappingRule(
        "MAP-DS-CSV-SRC-001",
        TargetItemType.DATAFLOW_GEN2,
        None,
        "CSV source dataset is configured as a source inside the Dataflow Gen2.",
    )


def map_sink_dataset(rejected: bool) -> MappingRule:
    """Sink dataset → Lakehouse table."""
    if rejected:
        return MappingRule(
            "MAP-DS-CSV-SINK-001",
            TargetItemType.LAKEHOUSE_TABLE,
            MigrationActionType.CREATE_TABLE,
            "CSV rejected sink becomes a Lakehouse rejected table.",
        )
    return MappingRule(
        "MAP-DS-PARQUET-SINK-001",
        TargetItemType.LAKEHOUSE_TABLE,
        MigrationActionType.CREATE_TABLE,
        "Parquet sink dataset becomes a Lakehouse table.",
    )


def map_data_flow() -> MappingRule:
    """ADF Mapping Data Flow → Fabric Dataflow Gen2."""
    return MappingRule(
        "MAP-DATAFLOW-001",
        TargetItemType.DATAFLOW_GEN2,
        MigrationActionType.CREATE_DATAFLOW,
        "Mapping Data Flow is rebuilt as a Fabric Dataflow Gen2.",
    )


def map_pipeline() -> MappingRule:
    """ADF pipeline → Fabric Data Pipeline."""
    return MappingRule(
        "MAP-PIPELINE-001",
        TargetItemType.DATA_PIPELINE,
        MigrationActionType.CREATE_PIPELINE,
        "ADF pipeline becomes a Fabric Data Pipeline.",
    )


def map_activity() -> MappingRule:
    """Pipeline activity → activity inside the Fabric Data Pipeline."""
    return MappingRule(
        "MAP-ACTIVITY-001",
        TargetItemType.DATA_PIPELINE,
        None,
        "Activity is recreated inside the Fabric Data Pipeline.",
    )


def map_trigger(trigger_type: str) -> MappingRule:
    """Schedule trigger → Fabric pipeline schedule."""
    if trigger_type == "ScheduleTrigger":
        return MappingRule(
            "MAP-TRG-SCHEDULE-001",
            TargetItemType.SCHEDULE,
            MigrationActionType.CONFIGURE_SCHEDULE,
            "Schedule trigger becomes a Fabric pipeline schedule.",
        )
    return MappingRule(
        "MAP-TRG-UNKNOWN-001",
        TargetItemType.NONE,
        None,
        f"Trigger type '{trigger_type}' has no Fabric mapping; handle manually.",
    )


# ── Concrete generated-definition rules ────────────────────────────


def _conversion(
    source_path: str,
    target_path: Optional[str],
    disposition: ConversionDisposition,
    note: str,
    source_value: Any = None,
    target_value: Any = None,
) -> PropertyConversion:
    return PropertyConversion(
        source_path=source_path,
        target_path=target_path,
        disposition=disposition,
        source_value=source_value,
        target_value=target_value,
        note=note,
    )


def connection_definition(linked_service: LinkedService) -> DefinitionSpec:
    endpoint = ""
    if linked_service.properties.type_properties:
        endpoint = linked_service.properties.type_properties.url or ""
    definition = {
        "type": "FabricConnection",
        "name": linked_service.name,
        "properties": {
            "connectionType": "AzureDataLakeStorageGen2",
            "endpoint": endpoint,
            "authentication": {"kind": "ManagedIdentity", "configured": False},
        },
    }
    return DefinitionSpec(
        definition,
        [
            _conversion("name", "name", ConversionDisposition.PRESERVED,
                        "Connection name preserved."),
            _conversion("properties.type", "properties.connectionType",
                        ConversionDisposition.CONVERTED,
                        "ADF AzureBlobFS converted to a Fabric ADLS Gen2 connection."),
            _conversion("properties.typeProperties.url", "properties.endpoint",
                        ConversionDisposition.RENAMED,
                        "ADF URL renamed to endpoint.", endpoint, endpoint),
            _conversion("properties.authentication", "properties.authentication",
                        ConversionDisposition.MANUAL,
                        "Authentication must be configured after package import."),
        ],
        ["Managed identity binding must be confirmed in the target workspace."],
        [],
        ["Configure and test the generated Fabric connection authentication."],
    )


def lakehouse_definition(name: str) -> DefinitionSpec:
    return DefinitionSpec(
        {
            "type": "Lakehouse",
            "name": name,
            "properties": {
                "description": "Lakehouse generated for migrated ADF outputs."
            },
        },
        [
            _conversion("migration.outputDatasets", "properties.description",
                        ConversionDisposition.CONVERTED,
                        "ADF sink datasets require a shared Lakehouse container.")
        ],
        [],
        [],
        [],
    )


def lakehouse_table_definition(
    dataset: Dataset, table_name: str, lakehouse_name: str
) -> DefinitionSpec:
    schema = dataset.properties.schema_def or []
    source_location = (
        dataset.properties.type_properties.location
        if dataset.properties.type_properties else None
    )
    definition = {
        "type": "LakehouseTable",
        "name": table_name,
        "properties": {
            "lakehouse": lakehouse_name,
            "format": "Delta",
            "schema": schema,
            "sourceDataset": dataset.name,
        },
    }
    conversions = [
        _conversion("name", "name",
                    ConversionDisposition.RENAMED if table_name != dataset.name
                    else ConversionDisposition.PRESERVED,
                    "Dataset prefix removed for the target table name.",
                    dataset.name, table_name),
        _conversion("properties.type", "properties.format",
                    ConversionDisposition.CONVERTED,
                    "ADF sink format converted to a Delta Lakehouse table.",
                    dataset.properties.type, "Delta"),
        _conversion("properties.schema", "properties.schema",
                    ConversionDisposition.PRESERVED,
                    "Declared source schema preserved.", schema, schema),
    ]
    if source_location:
        conversions.append(
            _conversion("properties.typeProperties.location", None,
                        ConversionDisposition.OMITTED_WITH_REASON,
                        "ADF storage path is replaced by the Lakehouse table location.",
                        source_location, None)
        )
    return DefinitionSpec(definition, conversions, [], [], [])


def dataflow_definition(
    dataflow: MappingDataFlow,
    connection_references: Optional[list[str]] = None,
) -> DefinitionSpec:
    tp = dataflow.properties.type_properties
    sources = [source.model_dump(by_alias=True, exclude_none=True) for source in tp.sources]
    sinks = [sink.model_dump(by_alias=True, exclude_none=True) for sink in tp.sinks]
    transformations = [
        transform.model_dump(by_alias=True, exclude_none=True)
        for transform in tp.transformations
    ]
    direct_connection_refs = sorted({
        ref.reference_name
        for node in [*tp.sources, *tp.sinks]
        for ref in [node.linked_service]
        if ref is not None
    })
    definition = {
        "type": "DataflowGen2",
        "name": dataflow.name,
        "properties": {
            "sources": sources,
            "transformations": transformations,
            "sinks": sinks,
            "connectionReferences": sorted(
                connection_references or direct_connection_refs
            ),
            "sourceScriptLines": tp.script_lines or (
                tp.script.splitlines() if tp.script else []
            ),
        },
    }
    return DefinitionSpec(
        definition,
        [
            _conversion("name", "name", ConversionDisposition.PRESERVED,
                        "Dataflow name preserved."),
            _conversion("properties.type", "type", ConversionDisposition.CONVERTED,
                        "ADF MappingDataFlow converted to Dataflow Gen2."),
            _conversion("properties.typeProperties.sources", "properties.sources",
                        ConversionDisposition.CONVERTED,
                        "ADF sources converted to Dataflow Gen2 source definitions."),
            _conversion("properties.typeProperties.transformations",
                        "properties.transformations", ConversionDisposition.CONVERTED,
                        "Transformations remain nested ordered components."),
            _conversion("properties.typeProperties.sinks", "properties.sinks",
                        ConversionDisposition.CONVERTED,
                        "ADF sinks converted to Dataflow Gen2 sink definitions."),
            _conversion("properties.typeProperties.scriptLines",
                        "properties.sourceScriptLines", ConversionDisposition.PRESERVED,
                        "Source script retained for conversion traceability."),
        ],
        (["Multiple sinks require structural review after conversion."]
         if len(sinks) > 1 else []),
        [],
        [],
    )


def _convert_activity(activity: dict[str, Any]) -> dict[str, Any]:
    converted = {
        "name": activity.get("name", "activity"),
        "type": activity.get("type", "Unknown"),
        "dependsOn": activity.get("dependsOn", []),
        "properties": dict(activity.get("typeProperties", {})),
    }
    if activity.get("type") == "ExecuteDataFlow":
        converted["type"] = "InvokeDataflowGen2"
        dataflow = activity.get("typeProperties", {}).get("dataFlow", {})
        converted["properties"] = {
            "dataflow": dataflow.get("referenceName", ""),
        }
    elif activity.get("type") == "IfCondition":
        properties = converted["properties"]
        properties["ifTrueActivities"] = [
            _convert_activity(item)
            for item in properties.get("ifTrueActivities", [])
        ]
        properties["ifFalseActivities"] = [
            _convert_activity(item)
            for item in properties.get("ifFalseActivities", [])
        ]
    if "policy" in activity:
        converted["policy"] = activity["policy"]
    return converted


def pipeline_definition(pipeline: ADFPipeline) -> DefinitionSpec:
    raw_activities = [
        activity.model_dump(by_alias=True, exclude_none=True)
        for activity in pipeline.properties.activities
    ]
    definition = {
        "type": "FabricDataPipeline",
        "name": pipeline.name,
        "properties": {
            "parameters": pipeline.properties.parameters or {},
            "variables": pipeline.properties.variables or {},
            "activities": [_convert_activity(item) for item in raw_activities],
            "description": pipeline.properties.description or "",
        },
    }
    return DefinitionSpec(
        definition,
        [
            _conversion("name", "name", ConversionDisposition.PRESERVED,
                        "Pipeline name preserved."),
            _conversion("properties.parameters", "properties.parameters",
                        ConversionDisposition.PRESERVED,
                        "Pipeline parameters preserved."),
            _conversion("properties.variables", "properties.variables",
                        ConversionDisposition.PRESERVED,
                        "Pipeline variables preserved."),
            _conversion("properties.activities", "properties.activities",
                        ConversionDisposition.CONVERTED,
                        "Activities converted as nested pipeline components."),
        ],
        ["ExecuteDataFlow activities are converted to InvokeDataflowGen2."],
        [],
        [],
    )


def schedule_definition(trigger: Trigger, pipeline_name: str) -> DefinitionSpec:
    recurrence = (
        trigger.properties.type_properties.recurrence
        if trigger.properties.type_properties else {}
    ) or {}
    parameters = (
        trigger.properties.pipelines[0].parameters
        if trigger.properties.pipelines else {}
    ) or {}
    definition = {
        "type": "FabricSchedule",
        "name": trigger.name,
        "properties": {
            "recurrence": recurrence,
            "pipeline": pipeline_name,
            "parameters": parameters,
            "enabled": False,
        },
    }
    return DefinitionSpec(
        definition,
        [
            _conversion("properties.type", "type", ConversionDisposition.CONVERTED,
                        "ADF ScheduleTrigger converted to FabricSchedule."),
            _conversion("properties.typeProperties.recurrence",
                        "properties.recurrence", ConversionDisposition.PRESERVED,
                        "Schedule recurrence preserved."),
            _conversion("properties.pipelines[0].pipelineReference",
                        "properties.pipeline", ConversionDisposition.RENAMED,
                        "Pipeline reference flattened to the generated pipeline name."),
            _conversion("properties.pipelines[0].parameters",
                        "properties.parameters", ConversionDisposition.PRESERVED,
                        "Trigger parameters preserved."),
            _conversion("properties.runtimeState", "properties.enabled",
                        ConversionDisposition.MANUAL,
                        "Generated schedules remain disabled until reviewed."),
        ],
        ["Generated schedule is disabled by default."],
        [],
        ["Review the recurrence and enable the schedule after deployment."],
    )


# ── Action kind by assessment status ─────────────────────────────

# READY = automatic; NEEDS_REVIEW = automatic + warning;
# REQUIRES_CHANGE = conversion; UNSUPPORTED = manual; BLOCKED = blocks.
AUTOMATIC = "automatic"
WARNING = "warning"
CONVERSION = "conversion"
MANUAL = "manual"
BLOCKED = "blocked"


def action_kind(status: AssessmentStatus) -> str:
    """Classify how an asset's assessment status shapes its action."""
    status = AssessmentStatus(status)
    if status == AssessmentStatus.READY:
        return AUTOMATIC
    if status == AssessmentStatus.NEEDS_REVIEW:
        return WARNING
    if status == AssessmentStatus.REQUIRES_CHANGE:
        return CONVERSION
    if status == AssessmentStatus.UNSUPPORTED:
        return MANUAL
    return BLOCKED
