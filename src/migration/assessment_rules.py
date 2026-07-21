"""Phase 4 compatibility rules for the mock ADF → Fabric workload.

Pure, deterministic rule functions with stable rule IDs. No I/O, no
Azure calls, no orchestration — each function maps a single asset
characteristic to a RuleOutcome. The engine (assessment.py) turns
those outcomes into AssessmentIssue / AssetAssessment objects.
"""

from typing import NamedTuple, Optional

from src.models.schemas import AssessmentStatus

READY = AssessmentStatus.READY
NEEDS_REVIEW = AssessmentStatus.NEEDS_REVIEW
REQUIRES_CHANGE = AssessmentStatus.REQUIRES_CHANGE
UNSUPPORTED = AssessmentStatus.UNSUPPORTED
BLOCKED = AssessmentStatus.BLOCKED


class RuleOutcome(NamedTuple):
    """The verdict of a single rule."""

    rule_id: str
    status: AssessmentStatus
    message: str
    recommended_action: str


# Credential-like keys that must never be embedded in a linked service.
# Fabric requires managed identity or Key Vault references instead.
CREDENTIAL_KEYS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "client_secret",
    "clientsecret",
    "accountkey",
    "account_key",
    "connectionstring",
    "connection_string",
    "accesstoken",
    "access_token",
    "sastoken",
    "sas_token",
    "serviceprincipalkey",
    "service_principal_key",
    "authkey",
    "auth_key",
}


# ── Pipeline activities ──────────────────────────────────────────

_ACTIVITY_RULES: dict[str, RuleOutcome] = {
    "GetMetadata": RuleOutcome(
        "ACT-GETMETADATA-001",
        READY,
        "GetMetadata activity is supported in Fabric pipelines.",
        "No change required.",
    ),
    "IfCondition": RuleOutcome(
        "ACT-IFCONDITION-001",
        READY,
        "IfCondition activity is supported in Fabric pipelines.",
        "No change required.",
    ),
    "Fail": RuleOutcome(
        "ACT-FAIL-001",
        READY,
        "Fail activity is supported in Fabric pipelines.",
        "No change required.",
    ),
    "ExecuteDataFlow": RuleOutcome(
        "ACT-EXECUTEDATAFLOW-001",
        REQUIRES_CHANGE,
        "ExecuteDataFlow must be re-pointed at a Fabric dataflow/notebook.",
        "Recreate the mapping data flow as a Fabric artifact and update the reference.",
    ),
}


def assess_activity(activity_type: str) -> RuleOutcome:
    """Assess a single pipeline activity by its type."""
    rule = _ACTIVITY_RULES.get(activity_type)
    if rule is not None:
        return rule
    return RuleOutcome(
        "ACT-UNKNOWN-001",
        UNSUPPORTED,
        f"Activity type '{activity_type}' has no known Fabric equivalent.",
        "Manually redesign this activity for Fabric.",
    )


# ── Data flow transformations ────────────────────────────────────

_READY_TRANSFORM_OPS = {"source", "sink", "filter", "derive"}
_CHANGE_TRANSFORM_OPS = {"join", "aggregate", "split"}


def classify_transformation(op: Optional[str], name: str) -> RuleOutcome:
    """Classify a data flow transformation by its script operation."""
    if op == "filter":
        return RuleOutcome(
            "DF-FILTER-001",
            READY,
            f"Filter transformation '{name}' maps cleanly to Fabric.",
            "No change required.",
        )
    if op == "derive":
        return RuleOutcome(
            "DF-DERIVE-001",
            READY,
            f"Derived-column transformation '{name}' maps cleanly to Fabric.",
            "No change required.",
        )
    if op == "join":
        return RuleOutcome(
            "DF-JOIN-001",
            REQUIRES_CHANGE,
            f"Join transformation '{name}' needs review of join semantics in Fabric.",
            "Recreate the join in the Fabric dataflow and validate output.",
        )
    if op == "aggregate":
        return RuleOutcome(
            "DF-AGGREGATE-001",
            REQUIRES_CHANGE,
            f"Aggregate transformation '{name}' needs review in Fabric.",
            "Recreate the aggregate in the Fabric dataflow and validate output.",
        )
    if op == "split":
        return RuleOutcome(
            "DF-SPLIT-001",
            REQUIRES_CHANGE,
            f"Conditional split '{name}' needs review in Fabric.",
            "Recreate the conditional split in the Fabric dataflow and validate branches.",
        )
    return RuleOutcome(
        "DF-XFORM-UNKNOWN-001",
        UNSUPPORTED,
        f"Transformation '{name}' (operation '{op}') has no known Fabric mapping.",
        "Manually redesign this transformation for Fabric.",
    )


def assess_sink_count(sink_count: int) -> Optional[RuleOutcome]:
    """Flag data flows with multiple sinks for manual review."""
    if sink_count > 1:
        return RuleOutcome(
            "DF-MULTISINK-001",
            NEEDS_REVIEW,
            f"Data flow writes to {sink_count} sinks; verify all targets in Fabric.",
            "Review each sink mapping after migration.",
        )
    return None


# ── Datasets ─────────────────────────────────────────────────────

_READY_DATASET_TYPES = {"DelimitedText", "Parquet"}


def assess_dataset(dataset_type: str, missing_linked_service: bool) -> RuleOutcome:
    """Assess a dataset by its type and linked-service resolution."""
    if missing_linked_service:
        return RuleOutcome(
            "DS-MISSING-LS-001",
            BLOCKED,
            "Dataset references a linked service that does not exist.",
            "Restore or recreate the linked service before migrating.",
        )
    if dataset_type == "DelimitedText":
        return RuleOutcome(
            "DS-CSV-001",
            READY,
            "Delimited text (CSV) dataset is supported in Fabric.",
            "No change required.",
        )
    if dataset_type == "Parquet":
        return RuleOutcome(
            "DS-PARQUET-001",
            READY,
            "Parquet dataset is supported in Fabric.",
            "No change required.",
        )
    return RuleOutcome(
        "DS-UNKNOWN-001",
        NEEDS_REVIEW,
        f"Dataset type '{dataset_type}' should be reviewed for Fabric support.",
        "Confirm the format is supported by the target Fabric item.",
    )


# ── Linked services ──────────────────────────────────────────────


def assess_linked_service(
    service_type: str, has_embedded_credential: bool
) -> RuleOutcome:
    """Assess a linked service by its type and credential handling."""
    if has_embedded_credential:
        return RuleOutcome(
            "LS-EMBEDDED-CRED-001",
            BLOCKED,
            "Linked service defines an embedded credential-like field.",
            "Replace the embedded credential with a managed identity or Key Vault reference.",
        )
    return RuleOutcome(
        "LS-ADLS-URL-001",
        NEEDS_REVIEW,
        f"Linked service '{service_type}' uses a URL/endpoint only; "
        "confirm the Fabric connection and authentication.",
        "Create the equivalent Fabric connection using managed identity.",
    )


# ── Triggers ─────────────────────────────────────────────────────


def assess_trigger(trigger_type: str) -> RuleOutcome:
    """Assess a trigger by its type."""
    if trigger_type == "ScheduleTrigger":
        return RuleOutcome(
            "TRG-SCHEDULE-001",
            NEEDS_REVIEW,
            "Schedule trigger must be recreated as a Fabric schedule.",
            "Recreate the schedule on the migrated Fabric pipeline.",
        )
    return RuleOutcome(
        "TRG-UNKNOWN-001",
        UNSUPPORTED,
        f"Trigger type '{trigger_type}' has no known Fabric equivalent.",
        "Manually redesign the trigger mechanism for Fabric.",
    )
