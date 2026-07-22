"""Fabric Migration Planner — Phase 5.

Turns a DiscoveryResult + AssessmentResult into a deterministic
MigrationPlan: source→target mappings, an ordered set of deployment
actions, manual work items, and post-migration validation rules.

No Fabric calls. No deployment. No approval. Deterministic: the same
inputs always produce the same plan content.
"""

import logging
from typing import NamedTuple, Optional

from src.migration import planning_rules as pr
from src.artifacts import build_package
from src.models.schemas import (
    ADFInventory,
    AssessmentResult,
    AssessmentStatus,
    DiscoveryResult,
    DeployableTargetType,
    GeneratedArtifact,
    GeneratedArtifactPackage,
    ManualAction,
    MigrationAction,
    MigrationActionType,
    MigrationPlan,
    MigrationPlanSummary,
    MigrationRisk,
    SourceTargetMapping,
    TargetItemType,
    ValidationRule,
    risk_for_status,
    worst_risk,
)

logger = logging.getLogger(__name__)

LAKEHOUSE_NAME = "lakehouse_migration_poc"
WORKSPACE_NAME = "fabric_workspace"
MONEY_TOLERANCE = 0.01
ROW_TOLERANCE = 0.0


class _Entry(NamedTuple):
    """Internal per-asset planning record."""

    source_asset: str
    source_type: str
    category: str  # linked_service|source_dataset|sink_dataset|data_flow|pipeline|activity|trigger
    rule: pr.MappingRule
    target_name: str
    status: AssessmentStatus


class MigrationPlanner:
    """Builds a Fabric migration plan from discovery + assessment."""

    def __init__(self, inventory: ADFInventory):
        self.inventory = inventory
        self._entries: list[_Entry] = []
        self._status: dict[str, AssessmentStatus] = {}
        self._source_refs: set[str] = set()
        self._sink_refs: set[str] = set()
        self._assessment_by_name = {}

    # ── Orchestration ────────────────────────────────────────────

    def generate_plan(
        self,
        discovery: DiscoveryResult,
        assessment: AssessmentResult,
        discovery_id: Optional[int] = None,
    ) -> MigrationPlan:
        """Generate the full migration plan (deterministic)."""
        self._status = {a.asset_name: a.status for a in assessment.assessments}
        self._assessment_by_name = {
            a.asset_name: a for a in assessment.assessments
        }
        self._compute_roles()
        self._entries = self._collect_entries()

        mappings = self.map_source_assets()
        actions = self.create_actions()
        manual_actions = self.create_manual_actions()
        validation_rules = self.create_validation_rules()
        generated_package = self.create_generated_package()

        executable = not any(
            AssessmentStatus(e.status) == AssessmentStatus.BLOCKED
            for e in self._entries
        )
        overall_risk = self.calculate_risk(actions, executable)
        summary = self.create_summary(
            mappings, actions, manual_actions, validation_rules,
            generated_package,
            executable, overall_risk,
        )

        logger.info(
            "Plan generated: %d mappings, %d actions, %d manual, "
            "%d validation rules, executable=%s, risk=%s.",
            len(mappings), len(actions), len(manual_actions),
            len(validation_rules), executable, overall_risk.value,
        )

        return MigrationPlan(
            discovery_id=discovery_id or assessment.discovery_id,
            executable=executable,
            overall_risk=overall_risk,
            assessment_status=assessment.overall_status,
            mappings=mappings,
            actions=actions,
            manual_actions=manual_actions,
            validation_rules=validation_rules,
            generated_package=generated_package,
            summary=summary,
        )

    # ── Concrete generated definitions ───────────────────────────────

    def create_generated_package(self) -> GeneratedArtifactPackage:
        """Generate concrete, deterministic Fabric artifact definitions."""
        artifacts: list[GeneratedArtifact] = []

        connection_ids = {
            linked_service.name: f"connection:{linked_service.name}"
            for linked_service in self.inventory.linked_services
        }
        for linked_service in self.inventory.linked_services:
            spec = pr.connection_definition(linked_service)
            artifacts.append(
                self._generated_artifact(
                    spec,
                    artifact_id=connection_ids[linked_service.name],
                    source_reference=f"linked_service:{linked_service.name}",
                    target_type=DeployableTargetType.CONNECTION,
                    target_name=linked_service.name,
                    source_names=[linked_service.name],
                )
            )

        sink_datasets = [
            dataset for dataset in self.inventory.datasets
            if dataset.name in self._sink_refs
        ]
        lakehouse_id = f"lakehouse:{LAKEHOUSE_NAME}"
        if sink_datasets:
            artifacts.append(
                self._generated_artifact(
                    pr.lakehouse_definition(LAKEHOUSE_NAME),
                    artifact_id=lakehouse_id,
                    source_reference="migration:output_datasets",
                    target_type=DeployableTargetType.LAKEHOUSE,
                    target_name=LAKEHOUSE_NAME,
                    source_names=[dataset.name for dataset in sink_datasets],
                )
            )

        table_ids: dict[str, str] = {}
        for dataset in sink_datasets:
            table_name = self._table_name(dataset.name)
            artifact_id = f"lakehouse_table:{table_name}"
            table_ids[dataset.name] = artifact_id
            connection_refs = self._dataset_connection_ids(
                dataset.name, connection_ids
            )
            artifacts.append(
                self._generated_artifact(
                    pr.lakehouse_table_definition(
                        dataset, table_name, LAKEHOUSE_NAME
                    ),
                    artifact_id=artifact_id,
                    source_reference=f"dataset:{dataset.name}",
                    target_type=DeployableTargetType.LAKEHOUSE_TABLE,
                    target_name=table_name,
                    dependencies=[lakehouse_id],
                    connection_references=connection_refs,
                    source_names=[dataset.name],
                )
            )

        dataflow_ids = {
            dataflow.name: f"dataflow:{dataflow.name}"
            for dataflow in self.inventory.data_flows
        }
        all_connection_ids = sorted(connection_ids.values())
        for dataflow in self.inventory.data_flows:
            dependencies = sorted(
                set(all_connection_ids) | set(table_ids.values())
            )
            artifacts.append(
                self._generated_artifact(
                    pr.dataflow_definition(dataflow, all_connection_ids),
                    artifact_id=dataflow_ids[dataflow.name],
                    source_reference=f"data_flow:{dataflow.name}",
                    target_type=DeployableTargetType.DATAFLOW_GEN2,
                    target_name=dataflow.name,
                    dependencies=dependencies,
                    connection_references=all_connection_ids,
                    source_names=[dataflow.name],
                )
            )

        pipeline_ids = {
            pipeline.name: f"pipeline:{pipeline.name}"
            for pipeline in self.inventory.pipelines
        }
        for pipeline in self.inventory.pipelines:
            activity_names = [
                activity.name
                for activity in self._walk_activities(
                    pipeline.properties.activities
                )
            ]
            artifacts.append(
                self._generated_artifact(
                    pr.pipeline_definition(pipeline),
                    artifact_id=pipeline_ids[pipeline.name],
                    source_reference=f"pipeline:{pipeline.name}",
                    target_type=DeployableTargetType.DATA_PIPELINE,
                    target_name=pipeline.name,
                    dependencies=sorted(dataflow_ids.values()),
                    source_names=[pipeline.name, *activity_names],
                )
            )

        default_pipeline = (
            self.inventory.pipelines[0].name
            if self.inventory.pipelines else "pipeline"
        )
        for trigger in self.inventory.triggers:
            pipeline_name = default_pipeline
            if (
                trigger.properties.pipelines
                and trigger.properties.pipelines[0].pipeline_reference
            ):
                pipeline_name = (
                    trigger.properties.pipelines[0]
                    .pipeline_reference.reference_name
                )
            dependency = pipeline_ids.get(
                pipeline_name, f"pipeline:{pipeline_name}"
            )
            artifacts.append(
                self._generated_artifact(
                    pr.schedule_definition(trigger, pipeline_name),
                    artifact_id=f"schedule:{trigger.name}",
                    source_reference=f"trigger:{trigger.name}",
                    target_type=DeployableTargetType.SCHEDULE,
                    target_name=trigger.name,
                    dependencies=[dependency],
                    source_names=[trigger.name],
                )
            )

        return build_package(artifacts)

    def _generated_artifact(
        self,
        spec: pr.DefinitionSpec,
        artifact_id: str,
        source_reference: str,
        target_type: DeployableTargetType,
        target_name: str,
        dependencies: Optional[list[str]] = None,
        connection_references: Optional[list[str]] = None,
        source_names: Optional[list[str]] = None,
    ) -> GeneratedArtifact:
        warnings = list(spec.warnings)
        unsupported = list(spec.unsupported_properties)
        manual = list(spec.manual_actions)
        for source_name in source_names or []:
            assessment = self._assessment_by_name.get(source_name)
            if assessment is None:
                continue
            for issue in assessment.issues:
                if issue.status != AssessmentStatus.READY:
                    warnings.append(f"{issue.rule_id}: {issue.message}")
                if issue.status in (
                    AssessmentStatus.UNSUPPORTED,
                    AssessmentStatus.BLOCKED,
                ):
                    unsupported.append(issue.rule_id)
                if issue.manual_review or issue.blocking:
                    manual.append(issue.recommended_action)
        return GeneratedArtifact(
            artifact_id=artifact_id,
            source_reference=source_reference,
            target_type=target_type,
            target_name=target_name,
            generated_definition=spec.definition,
            conversion_notes=spec.conversions,
            warnings=sorted(set(filter(None, warnings))),
            unsupported_properties=sorted(set(filter(None, unsupported))),
            manual_actions=sorted(set(filter(None, manual))),
            dependencies=sorted(set(dependencies or [])),
            connection_references=sorted(set(connection_references or [])),
            content_digest="",
        )

    def _dataset_connection_ids(
        self, dataset_name: str, connection_ids: dict[str, str]
    ) -> list[str]:
        dataset = next(
            (item for item in self.inventory.datasets if item.name == dataset_name),
            None,
        )
        if dataset is None or dataset.properties.linked_service_name is None:
            return []
        connection_name = dataset.properties.linked_service_name.reference_name
        return [connection_ids[connection_name]] if connection_name in connection_ids else []

    # ── Source→target mapping ────────────────────────────────────

    def map_source_assets(self) -> list[SourceTargetMapping]:
        """Map every source asset to a Fabric target or explain it."""
        mappings: list[SourceTargetMapping] = []
        for entry in self._entries:
            kind = pr.action_kind(entry.status)
            if kind in (pr.MANUAL, pr.BLOCKED):
                mappings.append(
                    SourceTargetMapping(
                        source_asset=entry.source_asset,
                        source_type=entry.source_type,
                        target_item_type=TargetItemType.NONE,
                        target_item_name="",
                        assessment_status=entry.status,
                        rule_id=entry.rule.rule_id,
                        mapped=False,
                        explanation=(
                            f"{entry.rule.explanation} "
                            "Handled as a manual action."
                        ),
                    )
                )
            else:
                mappings.append(
                    SourceTargetMapping(
                        source_asset=entry.source_asset,
                        source_type=entry.source_type,
                        target_item_type=entry.rule.target_item_type,
                        target_item_name=entry.target_name,
                        assessment_status=entry.status,
                        rule_id=entry.rule.rule_id,
                        mapped=True,
                        explanation=entry.rule.explanation,
                    )
                )
        return mappings

    # ── Deployment actions (ordered) ─────────────────────────────

    def create_actions(self) -> list[MigrationAction]:
        """Build the ordered list of deployment actions."""
        actions: list[MigrationAction] = []
        order = [0]  # mutable counter

        def add(action_type, target_type, target_name, source=None,
                source_type=None, reason="", status=None):
            order[0] += 1
            risk = (
                risk_for_status(status)
                if status is not None
                else (MigrationRisk.MEDIUM
                      if action_type == MigrationActionType.RUN_TARGET
                      else MigrationRisk.LOW)
            )
            kind = pr.action_kind(status) if status is not None else pr.AUTOMATIC
            actions.append(
                MigrationAction(
                    order=order[0],
                    action_type=action_type,
                    source_asset=source,
                    source_type=source_type,
                    target_item_type=target_type,
                    target_item_name=target_name,
                    risk=risk,
                    reason=reason,
                    approval_required=self._approval_required(action_type, status),
                    automated=kind not in (pr.MANUAL, pr.BLOCKED),
                    requires_conversion=(kind == pr.CONVERSION),
                    warning=(reason if kind == pr.WARNING else None),
                )
            )

        def mappable(entry: _Entry) -> bool:
            return (
                entry.rule.action_type is not None
                and pr.action_kind(entry.status) not in (pr.MANUAL, pr.BLOCKED)
            )

        # 1. Verify workspace
        add(MigrationActionType.VERIFY_WORKSPACE, TargetItemType.WORKSPACE,
            WORKSPACE_NAME, reason="Verify the target Fabric workspace exists.")

        # 2. Create connections
        for e in self._entries:
            if e.category == "linked_service" and mappable(e):
                add(MigrationActionType.CREATE_CONNECTION, TargetItemType.CONNECTION,
                    e.target_name, source=e.source_asset, source_type=e.source_type,
                    reason=e.rule.explanation, status=e.status)

        # 3. Create Lakehouse (only if there are tables to hold)
        if any(e.category == "sink_dataset" and mappable(e) for e in self._entries):
            add(MigrationActionType.CREATE_LAKEHOUSE, TargetItemType.LAKEHOUSE,
                LAKEHOUSE_NAME, reason="Create the Lakehouse for curated outputs.")

        # 4. Create target tables
        for e in self._entries:
            if e.category == "sink_dataset" and mappable(e):
                add(MigrationActionType.CREATE_TABLE, TargetItemType.LAKEHOUSE_TABLE,
                    e.target_name, source=e.source_asset, source_type=e.source_type,
                    reason=e.rule.explanation, status=e.status)

        # 5. Create Dataflow Gen2
        for e in self._entries:
            if e.category == "data_flow" and mappable(e):
                add(MigrationActionType.CREATE_DATAFLOW, TargetItemType.DATAFLOW_GEN2,
                    e.target_name, source=e.source_asset, source_type=e.source_type,
                    reason=e.rule.explanation, status=e.status)

        # 6. Create Fabric pipeline
        for e in self._entries:
            if e.category == "pipeline" and mappable(e):
                add(MigrationActionType.CREATE_PIPELINE, TargetItemType.DATA_PIPELINE,
                    e.target_name, source=e.source_asset, source_type=e.source_type,
                    reason=e.rule.explanation, status=e.status)

        # 7. Configure schedule
        for e in self._entries:
            if e.category == "trigger" and mappable(e):
                add(MigrationActionType.CONFIGURE_SCHEDULE, TargetItemType.SCHEDULE,
                    e.target_name, source=e.source_asset, source_type=e.source_type,
                    reason=e.rule.explanation, status=e.status)

        # 8. Run target (only if there is a pipeline to run)
        pipelines = [e for e in self._entries if e.category == "pipeline"]
        if pipelines:
            add(MigrationActionType.RUN_TARGET, TargetItemType.DATA_PIPELINE,
                pipelines[0].target_name,
                reason="Run the migrated Fabric pipeline once.")

        # 9. Validate
        add(MigrationActionType.VALIDATE, TargetItemType.NONE, "validation_suite",
            reason="Run post-migration validation rules.")

        return actions

    # ── Manual actions ───────────────────────────────────────────

    def create_manual_actions(self) -> list[ManualAction]:
        """Create manual work items for unsupported / blocked assets."""
        manual: list[ManualAction] = []
        for e in self._entries:
            status = AssessmentStatus(e.status)
            if status == AssessmentStatus.UNSUPPORTED:
                manual.append(
                    ManualAction(
                        source_asset=e.source_asset,
                        source_type=e.source_type,
                        reason="No Fabric equivalent; requires manual redesign.",
                        recommended_action=e.rule.explanation,
                        blocking=False,
                    )
                )
            elif status == AssessmentStatus.BLOCKED:
                manual.append(
                    ManualAction(
                        source_asset=e.source_asset,
                        source_type=e.source_type,
                        reason="Blocked dependency prevents automated migration.",
                        recommended_action=(
                            "Resolve the blocking issue before migrating."
                        ),
                        blocking=True,
                    )
                )
        return manual

    # ── Validation rules ─────────────────────────────────────────

    def create_validation_rules(self) -> list[ValidationRule]:
        """Create post-migration validation rules for the workload."""
        rules: list[ValidationRule] = []

        pipelines = [e for e in self._entries if e.category == "pipeline"]
        pipeline_name = pipelines[0].source_asset if pipelines else "pipeline"

        # Pipeline run status.
        rules.append(
            ValidationRule(
                name="pipeline_run_status",
                rule_type="run_status",
                source=f"adf:{pipeline_name}.run",
                target=f"fabric:{pipeline_name}.run",
                comparison="equals",
                tolerance=ROW_TOLERANCE,
                blocking=True,
            )
        )

        # Row-count checks, one per sink table.
        for e in self._entries:
            if e.category == "sink_dataset":
                rules.append(
                    ValidationRule(
                        name=f"{e.target_name}_row_count",
                        rule_type="row_count",
                        source=f"adf:{e.source_asset}",
                        target=f"lakehouse:{e.target_name}",
                        comparison="equals",
                        tolerance=ROW_TOLERANCE,
                        blocking=True,
                    )
                )

        # Output schema of the enriched output.
        rules.append(
            ValidationRule(
                name="output_schema",
                rule_type="schema",
                source="adf:enriched_orders.schema",
                target="lakehouse:enriched_orders.schema",
                comparison="equals",
                tolerance=ROW_TOLERANCE,
                blocking=True,
            )
        )

        # Money totals (tolerance 0.01).
        for money in ("gross", "discount", "net"):
            column = f"{money.capitalize()}Amount"
            rules.append(
                ValidationRule(
                    name=f"total_{money}_amount",
                    rule_type="sum",
                    source=f"adf:enriched_orders.{column}",
                    target=f"lakehouse:enriched_orders.{column}",
                    comparison="abs_diff_within_tolerance",
                    tolerance=MONEY_TOLERANCE,
                    blocking=True,
                )
            )

        # Customer-region grouped totals.
        rules.append(
            ValidationRule(
                name="customer_region_totals",
                rule_type="grouped_sum",
                source="adf:customer_summary.TotalNetAmount_by_region",
                target="lakehouse:customer_summary.TotalNetAmount_by_region",
                comparison="abs_diff_within_tolerance",
                tolerance=MONEY_TOLERANCE,
                blocking=True,
            )
        )

        # Runtime comparison (warning only)
        rules.append(
            ValidationRule(
                name="pipeline_runtime",
                rule_type="runtime",
                source=f"adf:{pipeline_name}.runtime",
                target=f"fabric:{pipeline_name}.runtime",
                comparison="within_tolerance",
                tolerance=0.2, # 20% tolerance
                blocking=False, # warning only
            )
        )

        return rules

    # ── Risk & summary ───────────────────────────────────────────

    def calculate_risk(
        self, actions: list[MigrationAction], executable: bool
    ) -> MigrationRisk:
        """Compute the overall plan risk."""
        if not executable:
            return MigrationRisk.CRITICAL
        candidates = [risk_for_status(e.status) for e in self._entries]
        candidates.extend(a.risk for a in actions)
        return worst_risk(candidates)

    def create_summary(
        self,
        mappings: list[SourceTargetMapping],
        actions: list[MigrationAction],
        manual_actions: list[ManualAction],
        validation_rules: list[ValidationRule],
        generated_package: GeneratedArtifactPackage,
        executable: bool,
        overall_risk: MigrationRisk,
    ) -> MigrationPlanSummary:
        """Build aggregate counts for the plan."""
        risk_counts = {r.value: 0 for r in MigrationRisk}
        for action in actions:
            risk_counts[MigrationRisk(action.risk).value] += 1

        target_counts: dict[str, int] = {}
        for mapping in mappings:
            if mapping.mapped:
                key = TargetItemType(mapping.target_item_type).value
                target_counts[key] = target_counts.get(key, 0) + 1

        return MigrationPlanSummary(
            total_source_assets=len(mappings),
            mapped_count=sum(1 for m in mappings if m.mapped),
            action_count=len(actions),
            manual_action_count=len(manual_actions),
            validation_rule_count=len(validation_rules),
            generated_artifact_count=len(generated_package.artifacts),
            executable=executable,
            overall_risk=overall_risk,
            risk_counts=risk_counts,
            target_item_counts=target_counts,
        )

    # ── Internal helpers ─────────────────────────────────────────

    def _compute_roles(self) -> None:
        """Determine which datasets are data-flow sources vs sinks."""
        self._source_refs = set()
        self._sink_refs = set()
        for df in self.inventory.data_flows:
            tp = df.properties.type_properties
            for src in tp.sources:
                if src.dataset:
                    self._source_refs.add(src.dataset.reference_name)
            for sink in tp.sinks:
                if sink.dataset:
                    self._sink_refs.add(sink.dataset.reference_name)

    def _status_of(self, name: str) -> AssessmentStatus:
        return self._status.get(name, AssessmentStatus.READY)

    def _collect_entries(self) -> list[_Entry]:
        """Build the canonical, ordered list of per-asset planning entries."""
        entries: list[_Entry] = []

        for ls in self.inventory.linked_services:
            entries.append(
                _Entry(ls.name, "linked_service", "linked_service",
                       pr.map_linked_service(), ls.name, self._status_of(ls.name))
            )

        for ds in self.inventory.datasets:
            is_sink = ds.name in self._sink_refs
            if is_sink:
                rejected = ds.properties.type == "DelimitedText"
                rule = pr.map_sink_dataset(rejected)
                entries.append(
                    _Entry(ds.name, "dataset", "sink_dataset", rule,
                           self._table_name(ds.name), self._status_of(ds.name))
                )
            else:
                rule = pr.map_source_dataset()
                entries.append(
                    _Entry(ds.name, "dataset", "source_dataset", rule,
                           self._primary_dataflow_name(), self._status_of(ds.name))
                )

        for df in self.inventory.data_flows:
            entries.append(
                _Entry(df.name, "data_flow", "data_flow",
                       pr.map_data_flow(), df.name, self._status_of(df.name))
            )

        for pl in self.inventory.pipelines:
            entries.append(
                _Entry(pl.name, "pipeline", "pipeline",
                       pr.map_pipeline(), pl.name, self._status_of(pl.name))
            )
            for activity in self._walk_activities(pl.properties.activities):
                entries.append(
                    _Entry(activity.name, "activity", "activity",
                           pr.map_activity(), pl.name,
                           self._status_of(activity.name))
                )

        for trg in self.inventory.triggers:
            entries.append(
                _Entry(trg.name, "trigger", "trigger",
                       pr.map_trigger(trg.properties.type),
                       trg.name, self._status_of(trg.name))
            )

        return entries

    def _primary_dataflow_name(self) -> str:
        if self.inventory.data_flows:
            return self.inventory.data_flows[0].name
        return "dataflow_gen2"

    @staticmethod
    def _table_name(dataset_name: str) -> str:
        """Derive a Lakehouse table name from a dataset name."""
        if dataset_name.startswith("ds_"):
            return dataset_name[3:]
        return dataset_name

    @staticmethod
    def _approval_required(
        action_type: MigrationActionType, status: Optional[AssessmentStatus]
    ) -> bool:
        """Decide whether an action needs human approval."""
        if action_type == MigrationActionType.RUN_TARGET:
            return True
        if status is not None and AssessmentStatus(status) != AssessmentStatus.READY:
            return True
        return False

    def _walk_activities(self, activities: list):
        """Flatten activities, recursing into IfCondition branches."""
        from src.models.schemas import PipelineActivity

        flat = []
        for activity in activities:
            flat.append(activity)
            if activity.type == "IfCondition":
                tp = activity.type_properties or {}
                for key in ("ifTrueActivities", "ifFalseActivities"):
                    parsed = []
                    for nested in tp.get(key, []):
                        try:
                            parsed.append(PipelineActivity(**nested))
                        except Exception:
                            continue
                    flat.extend(self._walk_activities(parsed))
        return flat
