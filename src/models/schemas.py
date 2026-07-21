"""Pydantic domain models for ADF resources.

Mirrors the Azure Data Factory JSON structure for pipelines, activities,
linked services, datasets, data flows, and triggers. All models allow
extra fields so unknown ADF properties don't break parsing.

No credential fields are defined. Serialization round-trips cleanly.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Shared ───────────────────────────────────────────────────────


class AssetReference(BaseModel):
    """Reference to another ADF asset (linked service, dataset, etc.)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    reference_name: str = Field(..., alias="referenceName")
    type: str = Field(default="LinkedServiceReference")


# ── Linked Services ──────────────────────────────────────────────


class LinkedServiceTypeProperties(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    url: Optional[str] = None


class LinkedServiceProperties(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: str
    type_properties: Optional[LinkedServiceTypeProperties] = Field(
        default=None, alias="typeProperties"
    )
    description: Optional[str] = None


class LinkedService(BaseModel):
    """ADF Linked Service definition."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    properties: LinkedServiceProperties


# ── Datasets ─────────────────────────────────────────────────────


class DatasetTypeProperties(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    location: Optional[dict[str, Any]] = None


class DatasetProperties(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: str
    linked_service_name: Optional[AssetReference] = Field(
        default=None, alias="linkedServiceName"
    )
    type_properties: Optional[DatasetTypeProperties] = Field(
        default=None, alias="typeProperties"
    )
    description: Optional[str] = None
    schema_def: Optional[list[dict[str, Any]]] = Field(
        default=None, alias="schema"
    )


class Dataset(BaseModel):
    """ADF Dataset definition."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    properties: DatasetProperties


# ── Data Flows ───────────────────────────────────────────────────


class DataFlowSource(BaseModel):
    """Source node in a Mapping Data Flow."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    description: Optional[str] = None
    dataset: Optional[AssetReference] = None
    linked_service: Optional[AssetReference] = Field(
        default=None, alias="linkedService"
    )


class DataFlowSink(BaseModel):
    """Sink node in a Mapping Data Flow."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    description: Optional[str] = None
    dataset: Optional[AssetReference] = None
    linked_service: Optional[AssetReference] = Field(
        default=None, alias="linkedService"
    )


class DataFlowTransformation(BaseModel):
    """Transformation step in a Mapping Data Flow."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    description: Optional[str] = None


class DataFlowScriptLines(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    script_lines: Optional[list[str]] = Field(
        default=None, alias="scriptLines"
    )
    script: Optional[str] = None


class MappingDataFlowTypeProperties(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    sources: list[DataFlowSource] = Field(default_factory=list)
    sinks: list[DataFlowSink] = Field(default_factory=list)
    transformations: list[DataFlowTransformation] = Field(
        default_factory=list
    )
    script_lines: Optional[list[str]] = Field(
        default=None, alias="scriptLines"
    )
    script: Optional[str] = None


class MappingDataFlowProperties(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: str = "MappingDataFlow"
    type_properties: MappingDataFlowTypeProperties = Field(
        ..., alias="typeProperties"
    )
    description: Optional[str] = None


class MappingDataFlow(BaseModel):
    """ADF Mapping Data Flow definition."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    properties: MappingDataFlowProperties


# ── Pipeline Activities ──────────────────────────────────────────


class ActivityDependency(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    activity: str
    dependency_conditions: list[str] = Field(
        default_factory=list, alias="dependencyConditions"
    )


class ActivityPolicy(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    timeout: Optional[str] = None
    retry: Optional[int] = None
    retry_interval_in_seconds: Optional[int] = Field(
        default=None, alias="retryIntervalInSeconds"
    )


class PipelineActivity(BaseModel):
    """Single activity inside an ADF pipeline."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    type: str
    depends_on: list[ActivityDependency] = Field(
        default_factory=list, alias="dependsOn"
    )
    policy: Optional[ActivityPolicy] = None
    type_properties: Optional[dict[str, Any]] = Field(
        default=None, alias="typeProperties"
    )
    inputs: Optional[list[AssetReference]] = None
    outputs: Optional[list[AssetReference]] = None


# ── Pipelines ────────────────────────────────────────────────────


class PipelineProperties(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    activities: list[PipelineActivity] = Field(default_factory=list)
    parameters: Optional[dict[str, Any]] = None
    variables: Optional[dict[str, Any]] = None
    description: Optional[str] = None


class ADFPipeline(BaseModel):
    """ADF Pipeline definition."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    properties: PipelineProperties


# ── Triggers ─────────────────────────────────────────────────────


class TriggerPipelineRef(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    pipeline_reference: Optional[AssetReference] = Field(
        default=None, alias="pipelineReference"
    )
    parameters: Optional[dict[str, Any]] = None


class TriggerTypeProperties(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    recurrence: Optional[dict[str, Any]] = None


class TriggerProperties(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: str
    type_properties: Optional[TriggerTypeProperties] = Field(
        default=None, alias="typeProperties"
    )
    pipelines: list[TriggerPipelineRef] = Field(default_factory=list)
    description: Optional[str] = None


class Trigger(BaseModel):
    """ADF Trigger definition."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    properties: TriggerProperties


# ── Full Inventory ───────────────────────────────────────────────


class ADFInventory(BaseModel):
    """Complete inventory of all ADF assets loaded from fixtures or discovery."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    pipelines: list[ADFPipeline] = Field(default_factory=list)
    linked_services: list[LinkedService] = Field(default_factory=list)
    datasets: list[Dataset] = Field(default_factory=list)
    data_flows: list[MappingDataFlow] = Field(default_factory=list)
    triggers: list[Trigger] = Field(default_factory=list)


# ── Discovery Result Models (Phase 3) ───────────────────────────


class DiscoveredAsset(BaseModel):
    """A single asset found during discovery."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    asset_type: str = Field(..., description="pipeline|dataset|linked_service|data_flow|trigger|activity")
    asset_name: str
    parent: Optional[str] = Field(default=None, description="Parent asset name (e.g. pipeline for activity)")
    metadata: dict[str, Any] = Field(default_factory=dict)


class DependencyEdge(BaseModel):
    """A directed dependency between two assets."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    source: str = Field(..., description="Dependent asset name")
    target: str = Field(..., description="Dependency target name")
    source_type: str = Field(..., description="Type of the source asset")
    target_type: str = Field(..., description="Type of the target asset")
    dependency_type: str = Field(
        ..., description="trigger_pipeline|pipeline_dataflow|pipeline_dataset|"
        "dataflow_dataset|dataset_linked_service"
    )


class MissingDependency(BaseModel):
    """A reference to an asset that does not exist in the inventory."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    source_asset: str
    source_type: str
    missing_reference: str
    expected_type: str
    dependency_type: str


class DiscoverySummary(BaseModel):
    """Aggregate counts from discovery."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    pipeline_count: int = 0
    activity_count: int = 0
    data_flow_count: int = 0
    source_count: int = 0
    sink_count: int = 0
    transformation_count: int = 0
    dataset_count: int = 0
    linked_service_count: int = 0
    trigger_count: int = 0
    dependency_count: int = 0
    missing_dependency_count: int = 0


class DiscoveryResult(BaseModel):
    """Complete output of the discovery engine."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    assets: list[DiscoveredAsset] = Field(default_factory=list)
    dependencies: list[DependencyEdge] = Field(default_factory=list)
    missing_dependencies: list[MissingDependency] = Field(default_factory=list)
    summary: DiscoverySummary = Field(default_factory=DiscoverySummary)


# ── Assessment Models (Phase 4) ─────────────────────────────────


class AssessmentStatus(str, Enum):
    """Compatibility verdict for an asset or issue.

    Ordered from most to least migratable. Overall status is the
    worst (highest priority) status across all assessed assets.
    """

    READY = "READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REQUIRES_CHANGE = "REQUIRES_CHANGE"
    UNSUPPORTED = "UNSUPPORTED"
    BLOCKED = "BLOCKED"


class IssueSeverity(str, Enum):
    """Severity of a single assessment issue."""

    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# Priority: higher number = worse. BLOCKED dominates, READY is lowest.
_STATUS_PRIORITY: dict[AssessmentStatus, int] = {
    AssessmentStatus.READY: 1,
    AssessmentStatus.NEEDS_REVIEW: 2,
    AssessmentStatus.REQUIRES_CHANGE: 3,
    AssessmentStatus.UNSUPPORTED: 4,
    AssessmentStatus.BLOCKED: 5,
}

_STATUS_SEVERITY: dict[AssessmentStatus, IssueSeverity] = {
    AssessmentStatus.READY: IssueSeverity.INFO,
    AssessmentStatus.NEEDS_REVIEW: IssueSeverity.LOW,
    AssessmentStatus.REQUIRES_CHANGE: IssueSeverity.MEDIUM,
    AssessmentStatus.UNSUPPORTED: IssueSeverity.HIGH,
    AssessmentStatus.BLOCKED: IssueSeverity.CRITICAL,
}

# Statuses that block automated migration outright.
_BLOCKING_STATUSES = {AssessmentStatus.BLOCKED, AssessmentStatus.UNSUPPORTED}


def status_priority(status: AssessmentStatus) -> int:
    """Return the priority rank of a status (higher = worse)."""
    return _STATUS_PRIORITY[AssessmentStatus(status)]


def severity_for_status(status: AssessmentStatus) -> IssueSeverity:
    """Map a status to its default issue severity."""
    return _STATUS_SEVERITY[AssessmentStatus(status)]


def is_blocking(status: AssessmentStatus) -> bool:
    """True if the status blocks automated migration (BLOCKED/UNSUPPORTED)."""
    return AssessmentStatus(status) in _BLOCKING_STATUSES


def is_manual_review(status: AssessmentStatus) -> bool:
    """True if the status needs a human to look at it (anything but READY)."""
    return AssessmentStatus(status) != AssessmentStatus.READY


def worst_status(statuses: Iterable[AssessmentStatus]) -> AssessmentStatus:
    """Return the highest-priority (worst) status; READY if empty."""
    worst = AssessmentStatus.READY
    for status in statuses:
        if status_priority(status) > status_priority(worst):
            worst = AssessmentStatus(status)
    return worst


class AssessmentIssue(BaseModel):
    """A single compatibility finding produced by a rule."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    rule_id: str
    asset_name: str
    asset_type: str
    status: AssessmentStatus
    severity: IssueSeverity
    message: str
    recommended_action: str = ""
    manual_review: bool = False
    blocking: bool = False


class AssetAssessment(BaseModel):
    """Assessment of a single asset — its worst status and all its issues."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    asset_name: str
    asset_type: str
    status: AssessmentStatus
    issues: list[AssessmentIssue] = Field(default_factory=list)


class AssessmentSummary(BaseModel):
    """Aggregate counts across an assessment run."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    total_assets: int = 0
    total_issues: int = 0
    ready_count: int = 0
    needs_review_count: int = 0
    requires_change_count: int = 0
    unsupported_count: int = 0
    blocked_count: int = 0
    blocking_issue_count: int = 0
    manual_review_issue_count: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)


class AssessmentResult(BaseModel):
    """Complete output of the compatibility assessment engine."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    overall_status: AssessmentStatus = AssessmentStatus.READY
    assessments: list[AssetAssessment] = Field(default_factory=list)
    summary: AssessmentSummary = Field(default_factory=AssessmentSummary)


# ── Migration Planning Models (Phase 5) ─────────────────────────


class TargetItemType(str, Enum):
    """Microsoft Fabric target item types."""

    WORKSPACE = "Workspace"
    CONNECTION = "FabricConnection"
    LAKEHOUSE = "Lakehouse"
    LAKEHOUSE_TABLE = "LakehouseTable"
    DATAFLOW_GEN2 = "DataflowGen2"
    DATA_PIPELINE = "FabricDataPipeline"
    SCHEDULE = "FabricSchedule"
    NONE = "None"


class MigrationActionType(str, Enum):
    """Ordered deployment step types for a migration plan."""

    VERIFY_WORKSPACE = "verify_workspace"
    CREATE_CONNECTION = "create_connection"
    CREATE_LAKEHOUSE = "create_lakehouse"
    CREATE_TABLE = "create_table"
    CREATE_DATAFLOW = "create_dataflow"
    CREATE_PIPELINE = "create_pipeline"
    CONFIGURE_SCHEDULE = "configure_schedule"
    RUN_TARGET = "run_target"
    VALIDATE = "validate"


class MigrationRisk(str, Enum):
    """Risk level of an action or of the overall plan."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


_RISK_PRIORITY: dict[MigrationRisk, int] = {
    MigrationRisk.LOW: 1,
    MigrationRisk.MEDIUM: 2,
    MigrationRisk.HIGH: 3,
    MigrationRisk.CRITICAL: 4,
}

_STATUS_RISK: dict[AssessmentStatus, MigrationRisk] = {
    AssessmentStatus.READY: MigrationRisk.LOW,
    AssessmentStatus.NEEDS_REVIEW: MigrationRisk.LOW,
    AssessmentStatus.REQUIRES_CHANGE: MigrationRisk.MEDIUM,
    AssessmentStatus.UNSUPPORTED: MigrationRisk.HIGH,
    AssessmentStatus.BLOCKED: MigrationRisk.CRITICAL,
}


def risk_priority(risk: MigrationRisk) -> int:
    """Return the priority rank of a risk (higher = worse)."""
    return _RISK_PRIORITY[MigrationRisk(risk)]


def risk_for_status(status: AssessmentStatus) -> MigrationRisk:
    """Map an assessment status to a migration risk level."""
    return _STATUS_RISK[AssessmentStatus(status)]


def worst_risk(risks: Iterable[MigrationRisk]) -> MigrationRisk:
    """Return the highest-priority (worst) risk; LOW if empty."""
    worst = MigrationRisk.LOW
    for risk in risks:
        if risk_priority(risk) > risk_priority(worst):
            worst = MigrationRisk(risk)
    return worst


class SourceTargetMapping(BaseModel):
    """Maps one source ADF asset to its Fabric target (or explains it)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    source_asset: str
    source_type: str
    target_item_type: TargetItemType
    target_item_name: str
    assessment_status: AssessmentStatus
    rule_id: str
    mapped: bool = True
    explanation: str = ""


class MigrationAction(BaseModel):
    """A single ordered deployment step in the migration plan."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    order: int
    action_type: MigrationActionType
    source_asset: Optional[str] = None
    source_type: Optional[str] = None
    target_item_type: TargetItemType
    target_item_name: str
    risk: MigrationRisk = MigrationRisk.LOW
    reason: str = ""
    approval_required: bool = False
    automated: bool = True
    requires_conversion: bool = False
    warning: Optional[str] = None


class ManualAction(BaseModel):
    """Work that cannot be automated (unsupported or blocked assets)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    source_asset: str
    source_type: str
    reason: str
    recommended_action: str
    blocking: bool = False


class ValidationRule(BaseModel):
    """A post-migration validation check comparing source and target."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    rule_type: str
    source: str
    target: str
    comparison: str
    tolerance: float = 0.0
    blocking: bool = True


class MigrationPlanSummary(BaseModel):
    """Aggregate counts for a migration plan."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    total_source_assets: int = 0
    mapped_count: int = 0
    action_count: int = 0
    manual_action_count: int = 0
    validation_rule_count: int = 0
    executable: bool = True
    overall_risk: MigrationRisk = MigrationRisk.LOW
    risk_counts: dict[str, int] = Field(default_factory=dict)
    target_item_counts: dict[str, int] = Field(default_factory=dict)


class MigrationPlan(BaseModel):
    """Complete Fabric migration plan derived from discovery + assessment."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    executable: bool = True
    overall_risk: MigrationRisk = MigrationRisk.LOW
    assessment_status: AssessmentStatus = AssessmentStatus.READY
    mappings: list[SourceTargetMapping] = Field(default_factory=list)
    actions: list[MigrationAction] = Field(default_factory=list)
    manual_actions: list[ManualAction] = Field(default_factory=list)
    validation_rules: list[ValidationRule] = Field(default_factory=list)
    summary: MigrationPlanSummary = Field(default_factory=MigrationPlanSummary)
