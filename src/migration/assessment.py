"""ADF → Fabric Compatibility Assessment Engine — Phase 4.

Consumes a DiscoveryResult (plus the source ADFInventory for detail)
and produces a deterministic AssessmentResult. Applies the rules in
assessment_rules.py to every asset and rolls the per-asset verdicts
up into an overall status.

No Fabric calls. No migration planning. Deterministic: the same
inventory always yields the same result.
"""

import logging
import re
from typing import Optional

from src.migration import assessment_rules as rules
from src.models.schemas import (
    ADFInventory,
    ADFPipeline,
    AssessmentIssue,
    AssessmentResult,
    AssessmentSummary,
    AssetAssessment,
    Dataset,
    DiscoveryResult,
    LinkedService,
    MappingDataFlow,
    MappingDataFlowTypeProperties,
    PipelineActivity,
    Trigger,
    is_blocking,
    is_manual_review,
    severity_for_status,
    worst_status,
)
from src.migration.assessment_rules import RuleOutcome

logger = logging.getLogger(__name__)

# Matches "<expr> ~> <TargetName>" segments in a Data Flow Script.
_STATEMENT_RE = re.compile(r"(.*?)~>\s*([A-Za-z_]\w*)", re.DOTALL)
# Matches the first "operation(" call inside an expression.
_OP_RE = re.compile(r"([A-Za-z_]\w*)\s*\(")


class ADFCompatibilityAssessment:
    """Assesses an ADF inventory for Microsoft Fabric compatibility."""

    def __init__(self, inventory: ADFInventory):
        self.inventory = inventory

    # ── Orchestration ────────────────────────────────────────────

    def assess_discovery(self, discovery: DiscoveryResult) -> AssessmentResult:
        """Assess every discovered asset and roll up an overall status.

        The DiscoveryResult drives which references are missing (used to
        mark datasets BLOCKED); asset detail comes from the inventory.
        """
        # Datasets whose linked-service reference was flagged missing.
        missing_ls_datasets = {
            m.source_asset
            for m in discovery.missing_dependencies
            if m.expected_type == "linked_service"
        }

        assessments: list[AssetAssessment] = []

        for ls in self.inventory.linked_services:
            assessments.append(self.assess_linked_service(ls))
        for ds in self.inventory.datasets:
            assessments.append(
                self.assess_dataset(ds, ds.name in missing_ls_datasets)
            )
        for df in self.inventory.data_flows:
            assessments.append(self.assess_dataflow(df))
        for pl in self.inventory.pipelines:
            assessments.extend(self.assess_pipeline(pl))
        for trg in self.inventory.triggers:
            assessments.append(self.assess_trigger(trg))

        overall = worst_status([a.status for a in assessments])
        summary = self.create_summary(assessments)

        result = AssessmentResult(
            overall_status=overall,
            assessments=assessments,
            summary=summary,
        )

        logger.info(
            "Assessment complete: %d assets, overall=%s, %d blocking issue(s).",
            len(assessments),
            overall.value,
            summary.blocking_issue_count,
        )
        return result

    # ── Per-asset assessment ─────────────────────────────────────

    def assess_linked_service(self, ls: LinkedService) -> AssetAssessment:
        """Assess a single linked service."""
        service_type = ls.properties.type
        outcome = rules.assess_linked_service(
            service_type, self._has_embedded_credential(ls)
        )
        issue = self._issue(outcome, ls.name, "linked_service")
        return AssetAssessment(
            asset_name=ls.name,
            asset_type="linked_service",
            status=outcome.status,
            issues=[issue],
        )

    def assess_dataset(
        self, dataset: Dataset, missing_linked_service: bool
    ) -> AssetAssessment:
        """Assess a single dataset."""
        outcome = rules.assess_dataset(
            dataset.properties.type, missing_linked_service
        )
        issue = self._issue(outcome, dataset.name, "dataset")
        return AssetAssessment(
            asset_name=dataset.name,
            asset_type="dataset",
            status=outcome.status,
            issues=[issue],
        )

    def assess_dataflow(self, dataflow: MappingDataFlow) -> AssetAssessment:
        """Assess a mapping data flow, one issue per transformation."""
        tp = dataflow.properties.type_properties
        op_map = self._parse_transformation_ops(tp)

        issues: list[AssessmentIssue] = []
        for transform in tp.transformations:
            op = op_map.get(transform.name)
            outcome = rules.classify_transformation(op, transform.name)
            issues.append(self._issue(outcome, dataflow.name, "data_flow"))

        multi_sink = rules.assess_sink_count(len(tp.sinks))
        if multi_sink is not None:
            issues.append(self._issue(multi_sink, dataflow.name, "data_flow"))

        status = worst_status([i.status for i in issues]) if issues else rules.READY
        return AssetAssessment(
            asset_name=dataflow.name,
            asset_type="data_flow",
            status=status,
            issues=issues,
        )

    def assess_pipeline(self, pipeline: ADFPipeline) -> list[AssetAssessment]:
        """Assess a pipeline container plus each of its activities.

        Returns one AssetAssessment for the pipeline itself and one for
        each activity, including activities nested inside IfCondition.
        """
        results: list[AssetAssessment] = []

        # The pipeline container itself migrates as-is.
        container_outcome = RuleOutcome(
            "PL-CONTAINER-001",
            rules.READY,
            "Pipeline container is supported in Fabric.",
            "No change required.",
        )
        results.append(
            AssetAssessment(
                asset_name=pipeline.name,
                asset_type="pipeline",
                status=container_outcome.status,
                issues=[self._issue(container_outcome, pipeline.name, "pipeline")],
            )
        )

        for activity in self._walk_activities(pipeline.properties.activities):
            outcome = rules.assess_activity(activity.type)
            results.append(
                AssetAssessment(
                    asset_name=activity.name,
                    asset_type="activity",
                    status=outcome.status,
                    issues=[self._issue(outcome, activity.name, "activity")],
                )
            )

        return results

    def assess_trigger(self, trigger: Trigger) -> AssetAssessment:
        """Assess a single trigger."""
        outcome = rules.assess_trigger(trigger.properties.type)
        issue = self._issue(outcome, trigger.name, "trigger")
        return AssetAssessment(
            asset_name=trigger.name,
            asset_type="trigger",
            status=outcome.status,
            issues=[issue],
        )

    # ── Summary ──────────────────────────────────────────────────

    def create_summary(
        self, assessments: list[AssetAssessment]
    ) -> AssessmentSummary:
        """Build aggregate counts from a list of asset assessments."""
        status_counts: dict[str, int] = {}
        for status in rules.AssessmentStatus:
            status_counts[status.value] = 0

        total_issues = 0
        blocking = 0
        manual = 0
        for assessment in assessments:
            status_counts[rules.AssessmentStatus(assessment.status).value] += 1
            for issue in assessment.issues:
                total_issues += 1
                if issue.blocking:
                    blocking += 1
                if issue.manual_review:
                    manual += 1

        return AssessmentSummary(
            total_assets=len(assessments),
            total_issues=total_issues,
            ready_count=status_counts[rules.READY.value],
            needs_review_count=status_counts[rules.NEEDS_REVIEW.value],
            requires_change_count=status_counts[rules.REQUIRES_CHANGE.value],
            unsupported_count=status_counts[rules.UNSUPPORTED.value],
            blocked_count=status_counts[rules.BLOCKED.value],
            blocking_issue_count=blocking,
            manual_review_issue_count=manual,
            status_counts=status_counts,
        )

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _issue(
        outcome: RuleOutcome, asset_name: str, asset_type: str
    ) -> AssessmentIssue:
        """Build an AssessmentIssue from a rule outcome."""
        return AssessmentIssue(
            rule_id=outcome.rule_id,
            asset_name=asset_name,
            asset_type=asset_type,
            status=outcome.status,
            severity=severity_for_status(outcome.status),
            message=outcome.message,
            recommended_action=outcome.recommended_action,
            manual_review=is_manual_review(outcome.status),
            blocking=is_blocking(outcome.status),
        )

    def _walk_activities(self, activities: list) -> list[PipelineActivity]:
        """Flatten activities, recursing into IfCondition branches."""
        flat: list[PipelineActivity] = []
        for activity in activities:
            flat.append(activity)
            if activity.type == "IfCondition":
                tp = activity.type_properties or {}
                nested_lists = [
                    tp.get("ifTrueActivities", []),
                    tp.get("ifFalseActivities", []),
                ]
                for nested_list in nested_lists:
                    parsed: list[PipelineActivity] = []
                    for nested in nested_list:
                        try:
                            parsed.append(PipelineActivity(**nested))
                        except Exception:
                            continue
                    flat.extend(self._walk_activities(parsed))
        return flat

    @staticmethod
    def _parse_transformation_ops(
        tp: MappingDataFlowTypeProperties,
    ) -> dict[str, str]:
        """Map each script target name to its operation keyword.

        Parses the Data Flow Script ("<expr> ~> <Name>" statements) and
        returns {target_name: operation}, e.g. {"JoinCustomers": "join"}.
        """
        lines = tp.script_lines
        if not lines and tp.script:
            lines = tp.script.splitlines()
        if not lines:
            return {}

        script = "\n".join(lines)
        op_map: dict[str, str] = {}
        for match in _STATEMENT_RE.finditer(script):
            expr, target = match.group(1), match.group(2)
            op_match = _OP_RE.search(expr)
            if op_match:
                op_map[target] = op_match.group(1)
        return op_map

    def _has_embedded_credential(self, ls: LinkedService) -> bool:
        """True if the linked service embeds a credential-like field."""
        data = ls.model_dump(by_alias=True)
        return self._scan_for_credentials(data)

    @staticmethod
    def _scan_for_credentials(data) -> bool:
        """Recursively check for credential-like keys with truthy values."""
        if isinstance(data, dict):
            for key, value in data.items():
                if (
                    key.lower() in rules.CREDENTIAL_KEYS
                    and value not in (None, "", {}, [])
                ):
                    return True
                if ADFCompatibilityAssessment._scan_for_credentials(value):
                    return True
        elif isinstance(data, list):
            for item in data:
                if ADFCompatibilityAssessment._scan_for_credentials(item):
                    return True
        return False
