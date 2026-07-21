"""Pydantic domain models for ADF resources.

Mirrors the Azure Data Factory JSON structure for pipelines, activities,
linked services, datasets, data flows, and triggers. All models allow
extra fields so unknown ADF properties don't break parsing.

No credential fields are defined. Serialization round-trips cleanly.
"""

from __future__ import annotations

from typing import Any, Optional

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
