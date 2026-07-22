"""Adapter: internal GeneratedArtifact definitions -> Fabric public-definition payloads.

Microsoft Fabric's item-definition API expects a ``parts`` list shaped like
the well-known Fabric Git-integration file conventions (``.platform``,
``pipeline-content.json``, ``mashup.pq`` + ``queryMetadata.json`` for
Dataflow Gen2, etc.). This PoC's internal ``GeneratedArtifact.generated_definition``
is a deterministic *internal* interchange format (see
``src/artifacts/schema_validation.py``) — it is **not** a valid Fabric public
definition and must never be sent to Fabric as-is.

This module is the only place that converts one into the other. Every
function is pure (no I/O, no network) and returns an ``AdapterResult``:

* ``deployable=True``  -> ``parts`` holds a valid Fabric definition payload.
* ``deployable=False`` -> ``reason`` explains why (surfaced as a distinct
  NON_DEPLOYABLE failure by the caller — never silently swapped for a fake
  definition).
"""

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

from src.artifacts import canonical_json
from src.models.schemas import DeployableTargetType, GeneratedArtifact

PLATFORM_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
    "platformProperties/2.0.0/schema.json"
)

# DeployableTargetType -> Fabric public item type name used in ".platform".
_PUBLIC_ITEM_TYPE = {
    DeployableTargetType.LAKEHOUSE: "Lakehouse",
    DeployableTargetType.DATAFLOW_GEN2: "DataflowGen2",
    DeployableTargetType.DATA_PIPELINE: "DataPipeline",
}


@dataclass
class AdapterResult:
    """Outcome of converting one artifact into a Fabric public definition."""

    deployable: bool
    item_type: str = ""
    parts: list[dict] = field(default_factory=list)
    reason: str = ""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _logical_id(artifact_id: str) -> str:
    """Deterministic pseudo-GUID derived from the artifact id (no randomness)."""
    digest = hashlib.sha256(artifact_id.encode("utf-8")).hexdigest()
    return "-".join(
        [digest[0:8], digest[8:12], digest[12:16], digest[16:20], digest[20:32]]
    )


def _platform_part(item_type: str, display_name: str, artifact_id: str) -> dict:
    """Build the ``.platform`` metadata part every Fabric item definition carries."""
    payload = {
        "$schema": PLATFORM_SCHEMA,
        "metadata": {"type": item_type, "displayName": display_name},
        "config": {"version": "2.0", "logicalId": _logical_id(artifact_id)},
    }
    text = canonical_json(payload)
    return {
        "path": ".platform",
        "payload": _b64(text),
        "payloadType": "InlineBase64",
    }


def _part(path: str, payload: Any) -> dict:
    text = canonical_json(payload) if not isinstance(payload, str) else payload
    return {
        "path": path,
        "payload": _b64(text),
        "payloadType": "InlineBase64",
    }


def build_lakehouse_definition(artifact: GeneratedArtifact) -> AdapterResult:
    """Lakehouse items only officially support the ``.platform`` descriptor
    part; there is no supported content definition for our internal
    'description' property, so only that part is produced."""
    parts = [
        _platform_part(
            _PUBLIC_ITEM_TYPE[DeployableTargetType.LAKEHOUSE],
            artifact.target_name,
            artifact.artifact_id,
        )
    ]
    return AdapterResult(
        deployable=True,
        item_type=_PUBLIC_ITEM_TYPE[DeployableTargetType.LAKEHOUSE],
        parts=parts,
    )


def build_pipeline_definition(artifact: GeneratedArtifact) -> AdapterResult:
    """Convert the internal pipeline definition into a Fabric Data Pipeline
    ``pipeline-content.json`` part. Fabric's Data Pipeline JSON format mirrors
    ADF pipeline JSON closely (`properties.activities/parameters/variables`),
    so this is a direct, faithful structural conversion — not a passthrough
    of the whole internal artifact."""
    properties = artifact.generated_definition.get("properties", {})
    if not isinstance(properties, dict):
        return AdapterResult(
            deployable=False,
            item_type=_PUBLIC_ITEM_TYPE[DeployableTargetType.DATA_PIPELINE],
            reason="Internal pipeline definition is missing a 'properties' object.",
        )
    activities = properties.get("activities")
    if not isinstance(activities, list):
        return AdapterResult(
            deployable=False,
            item_type=_PUBLIC_ITEM_TYPE[DeployableTargetType.DATA_PIPELINE],
            reason="Internal pipeline definition 'activities' must be a list.",
        )
    content = {
        "properties": {
            "activities": activities,
            "parameters": properties.get("parameters", {}) or {},
            "variables": properties.get("variables", {}) or {},
        }
    }
    parts = [
        _platform_part(
            _PUBLIC_ITEM_TYPE[DeployableTargetType.DATA_PIPELINE],
            artifact.target_name,
            artifact.artifact_id,
        ),
        _part("pipeline-content.json", content),
    ]
    return AdapterResult(
        deployable=True,
        item_type=_PUBLIC_ITEM_TYPE[DeployableTargetType.DATA_PIPELINE],
        parts=parts,
    )


def build_dataflow_definition(artifact: GeneratedArtifact) -> AdapterResult:
    """Fabric Dataflow Gen2 requires a real Power Query M document
    (``mashup.pq``) plus ``queryMetadata.json``. This PoC's discovery/planning
    layer only carries over the raw ADF Mapping Data Flow DSL script
    (`properties.sourceScriptLines`) — that is a *different* language than
    Power Query M, and there is no implemented MDF -> Power Query converter
    anywhere in this codebase. Sending the internal JSON (or the raw MDF
    script) to Fabric as if it were a Power Query mashup would silently
    corrupt the deployment, so this artifact type is marked NON_DEPLOYABLE
    until a real MDF -> Power Query converter exists.
    """
    properties = artifact.generated_definition.get("properties", {})
    compiled_pq = properties.get("compiledPowerQueryMashup") if isinstance(properties, dict) else None
    if not compiled_pq:
        return AdapterResult(
            deployable=False,
            item_type=_PUBLIC_ITEM_TYPE[DeployableTargetType.DATAFLOW_GEN2],
            reason=(
                "No Power Query M (mashup.pq) conversion is available for this "
                "Dataflow Gen2 artifact: the source is an ADF Mapping Data Flow "
                "script, which this PoC does not convert to Power Query M. "
                "Manual authoring of the Dataflow Gen2 is required."
            ),
        )
    parts = [
        _platform_part(
            _PUBLIC_ITEM_TYPE[DeployableTargetType.DATAFLOW_GEN2],
            artifact.target_name,
            artifact.artifact_id,
        ),
        _part("mashup.pq", compiled_pq),
        _part("queryMetadata.json", {"queryGroups": [], "queries": []}),
    ]
    return AdapterResult(
        deployable=True,
        item_type=_PUBLIC_ITEM_TYPE[DeployableTargetType.DATAFLOW_GEN2],
        parts=parts,
    )


_BUILDERS = {
    DeployableTargetType.LAKEHOUSE: build_lakehouse_definition,
    DeployableTargetType.DATAFLOW_GEN2: build_dataflow_definition,
    DeployableTargetType.DATA_PIPELINE: build_pipeline_definition,
}


def build_definition(artifact: GeneratedArtifact) -> AdapterResult:
    """Dispatch to the right builder for artifact.target_type.

    Raises ValueError for target types that do not have a workspace-item
    public definition at all (Connection/LakehouseTable/Schedule are handled
    by their own dedicated Fabric API shapes in FabricClient, never here).
    """
    builder = _BUILDERS.get(artifact.target_type)
    if builder is None:
        raise ValueError(
            f"No public-definition adapter for target type '{artifact.target_type}'."
        )
    return builder(artifact)


def definition_digest(parts: list[dict]) -> str:
    """Deterministic content digest of a Fabric definition parts list.

    Used to compare a freshly-built (approved) definition against a
    read-back definition fetched from Fabric after create/reuse.
    """
    normalized = sorted(
        ({"path": p["path"], "payload": p["payload"]} for p in parts),
        key=lambda p: p["path"],
    )
    return hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()
