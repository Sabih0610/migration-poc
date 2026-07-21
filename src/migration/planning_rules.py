"""Phase 5 source-to-target mapping rules (ADF → Microsoft Fabric).

Pure, deterministic mapping functions with stable rule IDs. Each
function returns a MappingRule describing the Fabric target for a
source asset category and, where applicable, the deployment action
that creates it. No orchestration, ordering, or I/O here.
"""

from typing import NamedTuple, Optional

from src.models.schemas import (
    AssessmentStatus,
    MigrationActionType,
    TargetItemType,
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
