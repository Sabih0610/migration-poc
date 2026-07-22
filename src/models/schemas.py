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
    # Exact JSON objects loaded from the source.  The typed fields above make
    # the definitions convenient to inspect; this payload guarantees that
    # unknown ADF properties survive discovery without normalization loss.
    source_definitions: dict[str, list[dict[str, Any]]] = Field(
        default_factory=dict
    )


# ── Discovery Result Models (Phase 3) ───────────────────────────


class SourceExpression(BaseModel):
    """An expression found within a source artifact or component."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    source_reference: str
    property_path: str
    value: Any
    expression_type: str = "Expression"


class ConnectionReference(BaseModel):
    """A source artifact reference to a linked service/connection."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    source_reference: str
    connection_name: str
    reference_type: str = "LinkedServiceReference"
    property_path: str = ""


class DiscoveredComponent(BaseModel):
    """A nested, non-deployable activity or transformation definition."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    component_id: str
    component_type: str = Field(..., description="activity|transformation")
    component_name: str
    parent_reference: str
    property_path: str
    order: int = 0
    definition: dict[str, Any] = Field(default_factory=dict)
    expressions: list[SourceExpression] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)


class DiscoveredAsset(BaseModel):
    """A single asset found during discovery."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    asset_type: str = Field(..., description="pipeline|dataset|linked_service|data_flow|trigger|activity")
    asset_name: str
    parent: Optional[str] = Field(default=None, description="Parent asset name (e.g. pipeline for activity)")
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_reference: str = ""
    definition: dict[str, Any] = Field(default_factory=dict)
    is_component: bool = False


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
    artifact_count: int = 0
    component_count: int = 0
    expression_count: int = 0
    connection_reference_count: int = 0


class DiscoveryResult(BaseModel):
    """Complete output of the discovery engine."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    assets: list[DiscoveredAsset] = Field(default_factory=list)
    dependencies: list[DependencyEdge] = Field(default_factory=list)
    missing_dependencies: list[MissingDependency] = Field(default_factory=list)
    summary: DiscoverySummary = Field(default_factory=DiscoverySummary)
    inventory: ADFInventory = Field(default_factory=ADFInventory)
    components: list[DiscoveredComponent] = Field(default_factory=list)
    expressions: list[SourceExpression] = Field(default_factory=list)
    connection_references: list[ConnectionReference] = Field(default_factory=list)


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

    discovery_id: Optional[int] = None
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


class DeployableTargetType(str, Enum):
    """Fabric item types that may appear in a generated artifact package."""

    CONNECTION = "FabricConnection"
    LAKEHOUSE = "Lakehouse"
    LAKEHOUSE_TABLE = "LakehouseTable"
    DATAFLOW_GEN2 = "DataflowGen2"
    DATA_PIPELINE = "FabricDataPipeline"
    SCHEDULE = "FabricSchedule"


class ConversionDisposition(str, Enum):
    """How a source property is represented in a generated definition."""

    PRESERVED = "PRESERVED"
    RENAMED = "RENAMED"
    CONVERTED = "CONVERTED"
    UNSUPPORTED = "UNSUPPORTED"
    MANUAL = "MANUAL"
    OMITTED_WITH_REASON = "OMITTED_WITH_REASON"


class PropertyConversion(BaseModel):
    """Traceability record for one source-to-target property conversion."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    source_path: str
    target_path: Optional[str] = None
    disposition: ConversionDisposition
    source_value: Any = None
    target_value: Any = None
    note: str = ""


class DefinitionSchemaResult(BaseModel):
    """Result of validating a generated definition against its schema."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    valid: bool
    schema_name: str = ""
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GeneratedArtifact(BaseModel):
    """Concrete generated Fabric artifact definition and its traceability."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    artifact_id: str
    source_reference: str
    target_type: DeployableTargetType
    target_name: str
    generated_definition: dict[str, Any]
    conversion_notes: list[PropertyConversion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    unsupported_properties: list[str] = Field(default_factory=list)
    manual_actions: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    connection_references: list[str] = Field(default_factory=list)
    content_digest: str


class ManifestEntry(BaseModel):
    """Index entry for one generated artifact file."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    artifact_id: str
    target_type: DeployableTargetType
    target_name: str
    relative_path: str
    content_digest: str
    dependencies: list[str] = Field(default_factory=list)


class ArtifactManifest(BaseModel):
    """Deterministic index of a generated artifact package."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    package_id: str
    schema_version: str = "1.0"
    plan_id: Optional[int] = None
    plan_version: Optional[int] = None
    entries: list[ManifestEntry] = Field(default_factory=list)
    package_digest: str = ""


class GeneratedArtifactPackage(BaseModel):
    """A collection of generated definitions and its manifest."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    package_id: str
    artifacts: list[GeneratedArtifact] = Field(default_factory=list)
    manifest: ArtifactManifest
    # Runtime location only: never serialized into plans, packages, or
    # approval fingerprints because it is machine-specific.
    output_directory: Optional[str] = Field(default=None, exclude=True)


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
    generated_artifact_count: int = 0
    executable: bool = True
    overall_risk: MigrationRisk = MigrationRisk.LOW
    risk_counts: dict[str, int] = Field(default_factory=dict)
    target_item_counts: dict[str, int] = Field(default_factory=dict)


class MigrationPlan(BaseModel):
    """Complete Fabric migration plan derived from discovery + assessment."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    discovery_id: Optional[int] = None
    executable: bool = True
    overall_risk: MigrationRisk = MigrationRisk.LOW
    assessment_status: AssessmentStatus = AssessmentStatus.READY
    mappings: list[SourceTargetMapping] = Field(default_factory=list)
    actions: list[MigrationAction] = Field(default_factory=list)
    manual_actions: list[ManualAction] = Field(default_factory=list)
    validation_rules: list[ValidationRule] = Field(default_factory=list)
    generated_package: Optional[GeneratedArtifactPackage] = None
    summary: MigrationPlanSummary = Field(default_factory=MigrationPlanSummary)


# ── Approval Models (Phase 6) ───────────────────────────────────


class ApprovalStatus(str, Enum):
    """Lifecycle state of a migration-plan approval."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    INVALIDATED = "INVALIDATED"


class ApprovalRequest(BaseModel):
    """Input to request approval for a plan."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    plan_id: int
    requested_by: str
    comment: str = ""


class ApprovalDecision(BaseModel):
    """Input to approve or reject an existing approval request."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    approval_id: int
    decided_by: str
    comment: str = ""


class ApprovalResult(BaseModel):
    """The full persisted state of an approval.

    Binds a decision to a specific plan id, version, and fingerprint so
    that any change to the plan invalidates a prior approval.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    approval_id: Optional[int] = None
    plan_id: int
    plan_version: int
    plan_fingerprint: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_by: str
    decided_by: Optional[str] = None
    request_comment: str = ""
    decision_comment: str = ""
    request_time: Optional[str] = None
    decision_time: Optional[str] = None


class ApprovalSummary(BaseModel):
    """Aggregate counts across approval requests."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    total: int = 0
    pending: int = 0
    approved: int = 0
    rejected: int = 0
    invalidated: int = 0


# ── Deployment Models (Phase 7) ─────────────────────────────────


class DeploymentMode(str, Enum):
    """How a plan is executed. REAL exists but is not implemented."""

    DRY_RUN = "DRY_RUN"
    MOCK = "MOCK"
    REAL = "REAL"


class DeploymentStatus(str, Enum):
    """Overall outcome of a deployment run."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"


class DeploymentStepStatus(str, Enum):
    """Outcome of a single deployment step."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class DeploymentStepResult(BaseModel):
    """Result of executing one plan action."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    order: int
    action_type: str
    artifact_id: Optional[str] = None
    target_item_type: str
    target_item_name: str
    content_digest: Optional[str] = None
    generated_definition: Optional[dict[str, Any]] = None
    status: DeploymentStepStatus
    resource_id: Optional[str] = None
    message: str = ""
    error: Optional[str] = None


class DeploymentSummary(BaseModel):
    """Aggregate counts for a deployment run."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    mode: DeploymentMode
    status: DeploymentStatus
    total_steps: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    resources_created: int = 0


class DeploymentResult(BaseModel):
    """Complete result of a (mock or dry-run) deployment."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    deployment_id: Optional[int] = None
    plan_id: int
    approval_id: int
    package_id: Optional[str] = None
    plan_fingerprint: Optional[str] = None
    mode: DeploymentMode
    status: DeploymentStatus
    steps: list[DeploymentStepResult] = Field(default_factory=list)
    summary: Optional[DeploymentSummary] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

# ── Phase 8: Validation & Reporting ──────────────────────────────

class ValidationStatus(str, Enum):
    PASSED = "PASSED"
    PASSED_WITH_WARNINGS = "PASSED_WITH_WARNINGS"
    FAILED = "FAILED"

class CheckStatus(str, Enum):
    PASSED = "PASSED"
    WARNING = "WARNING"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"

class DatasetMetrics(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    row_count: Optional[int] = None
    schema_hash: Optional[str] = None
    gross_total: Optional[float] = None
    discount_total: Optional[float] = None
    net_total: Optional[float] = None
    customer_region_totals: Optional[dict[str, float]] = None
    rejected_count: Optional[int] = None
    runtime_seconds: Optional[float] = None
    run_status: Optional[str] = None
    error: Optional[str] = None

class ValidationCheckResult(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    rule_name: str
    rule_type: str
    status: CheckStatus
    source_value: Any = None
    target_value: Any = None
    message: Optional[str] = None

class ValidationSummary(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    total_checks: int = 0
    passed: int = 0
    warnings: int = 0
    failed: int = 0
    skipped: int = 0

class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    validation_id: Optional[int] = None
    deployment_id: int
    plan_id: int
    status: ValidationStatus
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    summary: ValidationSummary
    source_metrics: dict[str, DatasetMetrics] = Field(default_factory=dict)
    target_metrics: dict[str, DatasetMetrics] = Field(default_factory=dict)
    checks: list[ValidationCheckResult] = Field(default_factory=list)


class StructuralValidationCheck(BaseModel):
    """One traceable artifact-definition validation assertion."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    category: str
    status: CheckStatus
    message: str
    source_reference: Optional[str] = None
    target_artifact_id: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)


class StructuralValidationSummary(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    total_checks: int = 0
    passed: int = 0
    warnings: int = 0
    failed: int = 0
    skipped: int = 0
    category_counts: dict[str, int] = Field(default_factory=dict)


class StructuralValidationResult(BaseModel):
    """Comparison of source snapshot, approved package, and deployment."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    validation_id: Optional[int] = None
    discovery_id: int
    deployment_id: int
    plan_id: int
    approval_id: int
    package_fingerprint: str
    status: ValidationStatus
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    summary: StructuralValidationSummary
    checks: list[StructuralValidationCheck] = Field(default_factory=list)

class MigrationReport(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    report_id: str
    generated_at: str
    workflow_stages: dict[str, Any] = Field(default_factory=dict)
    source_artifacts: list[Any] = Field(default_factory=list)
    generated_artifacts: list[Any] = Field(default_factory=list)
    mappings: list[Any] = Field(default_factory=list)
    property_conversions: list[Any] = Field(default_factory=list)
    unsupported_properties: list[Any] = Field(default_factory=list)
    manual_actions: list[Any] = Field(default_factory=list)
    approval: Any = None
    deployment: Any = None
    structural_validation: StructuralValidationResult
    runtime_validation: Any = None
    runtime_execution_validation: Any = None


# ── Phase 11: Controlled execution + runtime-equivalence validation ─────


class ExecutionSide(str, Enum):
    """Which side of the migration a controlled execution ran on."""

    SOURCE = "source"
    TARGET = "target"


class ExecutionStatus(str, Enum):
    """Lifecycle state of one controlled pipeline execution."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class RuntimeMetrics(BaseModel):
    """Safe, structure-only runtime metrics for one execution.

    Deliberately has no free-form raw-row field: only counts, schema
    structure, and configured aggregate totals are ever collected.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    status: Optional[str] = None
    schemas: dict[str, str] = Field(default_factory=dict)
    total_row_count: Optional[int] = None
    valid_row_count: Optional[int] = None
    rejected_row_count: Optional[int] = None
    numeric_totals: dict[str, float] = Field(default_factory=dict)
    grouped_totals: dict[str, dict[str, float]] = Field(default_factory=dict)
    null_counts: dict[str, int] = Field(default_factory=dict)
    duplicate_counts: dict[str, int] = Field(default_factory=dict)
    duration_seconds: Optional[float] = None


class PipelineExecutionResult(BaseModel):
    """Safe metadata + collected metrics for one controlled execution."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    execution_id: Optional[int] = None
    correlation_id: str
    side: ExecutionSide
    pipeline_identity: str
    run_id: Optional[str] = None
    plan_id: Optional[int] = None
    deployment_id: Optional[int] = None
    discovery_snapshot_id: Optional[int] = None
    status: ExecutionStatus
    safe_error_category: Optional[str] = None
    safe_error_message: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    metrics: Optional[RuntimeMetrics] = None


class RuntimeValidationRuleConfig(BaseModel):
    """Safe, config-only comparison rules (column names / aggregate types)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    required_metrics: list[str] = Field(default_factory=list)
    row_count_tolerance: int = 0
    numeric_total_tolerance: float = 0.0
    grouped_total_tolerance: float = 0.0
    allow_duration_warning: bool = True
    duration_tolerance_pct: float = 0.2


class RuntimeValidationStatus(str, Enum):
    """Overall (and per-check) outcome of a runtime-equivalence validation."""

    PASS = "PASS"
    PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"
    INCONCLUSIVE = "INCONCLUSIVE"


class RuntimeValidationCheckResult(BaseModel):
    """One traceable runtime-metric comparison assertion."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    status: RuntimeValidationStatus
    source_value: Any = None
    target_value: Any = None
    tolerance: Optional[float] = None
    explanation: str = ""


class RuntimeValidationSummary(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    total_checks: int = 0
    passed: int = 0
    warnings: int = 0
    failed: int = 0
    inconclusive: int = 0


class RuntimeValidationResult(BaseModel):
    """Optional, execution-linked runtime-equivalence validation result.

    Strictly additive to structural validation: running or failing a
    runtime validation must never modify structural validation status.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    validation_id: Optional[int] = None
    discovery_snapshot_id: Optional[int] = None
    plan_id: int
    plan_version: int
    package_fingerprint: str
    deployment_id: int
    source_execution_id: int
    source_run_id: Optional[str] = None
    target_execution_id: int
    target_run_id: Optional[str] = None
    correlation_id: str
    status: RuntimeValidationStatus
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    summary: RuntimeValidationSummary = Field(default_factory=RuntimeValidationSummary)
    checks: list[RuntimeValidationCheckResult] = Field(default_factory=list)
