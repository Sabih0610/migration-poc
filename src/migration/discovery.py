"""ADF Discovery Service — Phase 3.

Scans an ADFInventory and produces a DiscoveryResult containing
all discovered assets, dependency edges, and missing references.

Uses mock fixtures only. No Azure calls. No assessment logic.
"""

import logging
import re
from typing import Any

from src.models.schemas import (
    ADFInventory,
    ConnectionReference,
    DependencyEdge,
    DiscoveredAsset,
    DiscoveredComponent,
    DiscoveryResult,
    DiscoverySummary,
    MissingDependency,
    PipelineActivity,
    SourceExpression,
)

logger = logging.getLogger(__name__)

_STATEMENT_RE = re.compile(r"(.*?)~>\s*([A-Za-z_]\w*)", re.DOTALL)


class ADFDiscoveryService:
    """Discovers assets and dependencies from an ADF inventory."""

    def __init__(self, inventory: ADFInventory):
        self.inventory = inventory
        self._assets: list[DiscoveredAsset] = []
        self._dependencies: list[DependencyEdge] = []
        self._missing: list[MissingDependency] = []
        self._components: list[DiscoveredComponent] = []
        self._expressions: list[SourceExpression] = []
        self._connection_references: list[ConnectionReference] = []

        # Lookup sets for reference validation
        self._ls_names: set[str] = {ls.name for ls in inventory.linked_services}
        self._ds_names: set[str] = {ds.name for ds in inventory.datasets}
        self._df_names: set[str] = {df.name for df in inventory.data_flows}
        self._pl_names: set[str] = {pl.name for pl in inventory.pipelines}

    def scan_inventory(self) -> DiscoveryResult:
        """Run full discovery and return results."""
        self._assets.clear()
        self._dependencies.clear()
        self._missing.clear()
        self._components.clear()
        self._expressions.clear()
        self._connection_references.clear()

        self.discover_assets()
        self.discover_expressions_and_connections()
        self.discover_pipeline_dependencies()
        self.discover_dataflow_dependencies()
        self.discover_transformation_dependencies()
        self.discover_trigger_dependencies()
        self.find_missing_dependencies()

        summary = self.create_summary()

        result = DiscoveryResult(
            assets=list(self._assets),
            dependencies=list(self._dependencies),
            missing_dependencies=list(self._missing),
            summary=summary,
            inventory=self.inventory,
            components=list(self._components),
            expressions=list(self._expressions),
            connection_references=list(self._connection_references),
        )

        logger.info(
            "Discovery complete: %d assets, %d dependencies, %d missing.",
            len(self._assets),
            len(self._dependencies),
            len(self._missing),
        )

        return result

    def discover_assets(self) -> None:
        """Discover top-level artifacts and their nested components."""
        # Linked services
        for ls in self.inventory.linked_services:
            self._assets.append(
                DiscoveredAsset(
                    asset_type="linked_service",
                    asset_name=ls.name,
                    metadata={"service_type": ls.properties.type},
                    source_reference=f"linked_service:{ls.name}",
                    definition=self._raw_definition(
                        "linked_services", ls.name, ls
                    ),
                )
            )

        # Datasets
        for ds in self.inventory.datasets:
            self._assets.append(
                DiscoveredAsset(
                    asset_type="dataset",
                    asset_name=ds.name,
                    metadata={"dataset_type": ds.properties.type},
                    source_reference=f"dataset:{ds.name}",
                    definition=self._raw_definition("datasets", ds.name, ds),
                )
            )

        # Data flows
        for df in self.inventory.data_flows:
            tp = df.properties.type_properties
            self._assets.append(
                DiscoveredAsset(
                    asset_type="data_flow",
                    asset_name=df.name,
                    metadata={
                        "source_count": len(tp.sources),
                        "sink_count": len(tp.sinks),
                        "transformation_count": len(tp.transformations),
                    },
                    source_reference=f"data_flow:{df.name}",
                    definition=self._raw_definition("data_flows", df.name, df),
                )
            )
            statements = self._transformation_statements(tp.script_lines or [])
            for order, transform in enumerate(tp.transformations, start=1):
                definition = transform.model_dump(by_alias=True, exclude_none=True)
                if transform.name in statements:
                    definition["scriptStatement"] = statements[transform.name]
                self._components.append(
                    DiscoveredComponent(
                        component_id=(
                            f"data_flow:{df.name}/transformation:{transform.name}"
                        ),
                        component_type="transformation",
                        component_name=transform.name,
                        parent_reference=f"data_flow:{df.name}",
                        property_path=(
                            f"properties.typeProperties.transformations[{order - 1}]"
                        ),
                        order=order,
                        definition=definition,
                    )
                )

        # Pipelines and their activities
        for pl in self.inventory.pipelines:
            self._assets.append(
                DiscoveredAsset(
                    asset_type="pipeline",
                    asset_name=pl.name,
                    metadata={
                        "activity_count": len(pl.properties.activities),
                        "parameters": pl.properties.parameters or {},
                        "variables": pl.properties.variables or {},
                    },
                    source_reference=f"pipeline:{pl.name}",
                    definition=self._raw_definition("pipelines", pl.name, pl),
                )
            )
            self._discover_activity_components(
                pipeline_name=pl.name,
                activities=pl.properties.activities,
                property_path="properties.activities",
            )

        # Triggers
        for trg in self.inventory.triggers:
            self._assets.append(
                DiscoveredAsset(
                    asset_type="trigger",
                    asset_name=trg.name,
                    metadata={"trigger_type": trg.properties.type},
                    source_reference=f"trigger:{trg.name}",
                    definition=self._raw_definition("triggers", trg.name, trg),
                )
            )

    def _discover_activity_components(
        self,
        pipeline_name: str,
        activities: list[PipelineActivity],
        property_path: str,
    ) -> None:
        """Register activities recursively as non-deployable components.

        Activity entries remain in ``assets`` for backward-compatible API
        consumers, but ``is_component`` makes their non-deployable nature
        explicit and the complete normalized record lives in ``components``.
        """
        for order, activity in enumerate(activities, start=1):
            path = f"{property_path}[{order - 1}]"
            source_ref = f"pipeline:{pipeline_name}/activity:{activity.name}"
            expressions = self._extract_expressions(
                activity.model_dump(by_alias=True, exclude_none=True),
                source_ref,
                path,
            )
            dependencies = [d.activity for d in activity.depends_on]
            definition = activity.model_dump(by_alias=True, exclude_none=True)
            self._components.append(
                DiscoveredComponent(
                    component_id=source_ref,
                    component_type="activity",
                    component_name=activity.name,
                    parent_reference=f"pipeline:{pipeline_name}",
                    property_path=path,
                    order=order,
                    definition=definition,
                    expressions=expressions,
                    dependencies=dependencies,
                )
            )
            self._assets.append(
                DiscoveredAsset(
                    asset_type="activity",
                    asset_name=activity.name,
                    parent=pipeline_name,
                    metadata={"activity_type": activity.type},
                    source_reference=source_ref,
                    definition=definition,
                    is_component=True,
                )
            )

            if activity.type == "IfCondition":
                tp = activity.type_properties or {}
                for key in ("ifTrueActivities", "ifFalseActivities"):
                    parsed: list[PipelineActivity] = []
                    for nested in tp.get(key, []):
                        try:
                            parsed.append(PipelineActivity(**nested))
                        except Exception:
                            logger.warning(
                                "Could not parse nested activity in %s.%s.",
                                activity.name,
                                key,
                            )
                    self._discover_activity_components(
                        pipeline_name,
                        parsed,
                        f"{path}.typeProperties.{key}",
                    )

    def discover_pipeline_dependencies(self) -> None:
        """Discover dependencies within pipelines."""
        for pl in self.inventory.pipelines:
            self._discover_activities_recursive(
                pl.name, pl.properties.activities
            )

    def _discover_activities_recursive(
        self, pipeline_name: str, activities: list
    ) -> None:
        """Walk activities including nested ones inside IfCondition."""
        for act in activities:
            tp = act.type_properties or {}

            # Activity dependsOn references are definition dependencies.
            for dependency in act.depends_on:
                self._dependencies.append(
                    DependencyEdge(
                        source=act.name,
                        target=dependency.activity,
                        source_type="activity",
                        target_type="activity",
                        dependency_type="activity_dependency",
                        dependency_conditions=dependency.dependency_conditions,
                    )
                )

            # ExecuteDataFlow → data flow reference
            if act.type == "ExecuteDataFlow":
                df_ref = tp.get("dataFlow", {})
                ref_name = df_ref.get("referenceName", "")
                if ref_name:
                    self._dependencies.append(
                        DependencyEdge(
                            source=pipeline_name,
                            target=ref_name,
                            source_type="pipeline",
                            target_type="data_flow",
                            dependency_type="pipeline_dataflow",
                        )
                    )

            # GetMetadata → dataset reference
            if act.type == "GetMetadata":
                ds_ref = tp.get("dataset", {})
                ref_name = ds_ref.get("referenceName", "")
                if ref_name:
                    self._dependencies.append(
                        DependencyEdge(
                            source=act.name,
                            target=ref_name,
                            source_type="activity",
                            target_type="dataset",
                            dependency_type="pipeline_dataset",
                        )
                    )

            # Activity inputs/outputs
            if act.inputs:
                for inp in act.inputs:
                    self._dependencies.append(
                        DependencyEdge(
                            source=act.name,
                            target=inp.reference_name,
                            source_type="activity",
                            target_type="dataset",
                            dependency_type="pipeline_dataset",
                        )
                    )
            if act.outputs:
                for out in act.outputs:
                    self._dependencies.append(
                        DependencyEdge(
                            source=act.name,
                            target=out.reference_name,
                            source_type="activity",
                            target_type="dataset",
                            dependency_type="pipeline_dataset",
                        )
                    )

            # IfCondition — recurse into nested activities
            if act.type == "IfCondition":
                true_acts = tp.get("ifTrueActivities", [])
                false_acts = tp.get("ifFalseActivities", [])
                for nested_list in [true_acts, false_acts]:
                    parsed = []
                    for nested in nested_list:
                        try:
                            parsed.append(PipelineActivity(**nested))
                        except Exception:
                            pass
                    if parsed:
                        for nested_act in parsed:
                            self._dependencies.append(
                                DependencyEdge(
                                    source=nested_act.name,
                                    target=act.name,
                                    source_type="activity",
                                    target_type="activity",
                                    dependency_type="activity_parent",
                                )
                            )
                        self._discover_activities_recursive(
                            pipeline_name, parsed
                        )

    def discover_dataflow_dependencies(self) -> None:
        """Discover dataset dependencies from data flow sources and sinks."""
        for df in self.inventory.data_flows:
            tp = df.properties.type_properties

            for src in tp.sources:
                if src.dataset:
                    self._dependencies.append(
                        DependencyEdge(
                            source=df.name,
                            target=src.dataset.reference_name,
                            source_type="data_flow",
                            target_type="dataset",
                            dependency_type="dataflow_dataset",
                        )
                    )

            for sink in tp.sinks:
                if sink.dataset:
                    self._dependencies.append(
                        DependencyEdge(
                            source=df.name,
                            target=sink.dataset.reference_name,
                            source_type="data_flow",
                            target_type="dataset",
                            dependency_type="dataflow_dataset",
                        )
                    )

    def discover_transformation_dependencies(self) -> None:
        """Capture deterministic transformation component order."""
        for dataflow in self.inventory.data_flows:
            transforms = dataflow.properties.type_properties.transformations
            for index, transform in enumerate(transforms):
                if index == 0:
                    continue
                self._dependencies.append(
                    DependencyEdge(
                        source=transform.name,
                        target=transforms[index - 1].name,
                        source_type="transformation",
                        target_type="transformation",
                        dependency_type="transformation_order",
                    )
                )

    def discover_expressions_and_connections(self) -> None:
        """Extract normalized expression and linked-service references."""
        category_types = {
            "pipelines": "pipeline",
            "linked_services": "linked_service",
            "datasets": "dataset",
            "data_flows": "data_flow",
            "triggers": "trigger",
        }
        seen_connections: set[tuple[str, str, str]] = set()
        for category, definitions in self.inventory.source_definitions.items():
            asset_type = category_types.get(category, category.rstrip("s"))
            for definition in definitions:
                name = definition.get("name", "unknown")
                source_ref = f"{asset_type}:{name}"
                self._extract_expressions(
                    definition, source_ref, "", register=True
                )
                for ref in self._extract_connection_references(
                    definition, source_ref
                ):
                    key = (
                        ref.source_reference,
                        ref.connection_name,
                        ref.property_path,
                    )
                    if key not in seen_connections:
                        seen_connections.add(key)
                        self._connection_references.append(ref)

    def discover_trigger_dependencies(self) -> None:
        """Discover pipeline references from triggers."""
        for trg in self.inventory.triggers:
            for pipe_ref in trg.properties.pipelines:
                if pipe_ref.pipeline_reference:
                    self._dependencies.append(
                        DependencyEdge(
                            source=trg.name,
                            target=pipe_ref.pipeline_reference.reference_name,
                            source_type="trigger",
                            target_type="pipeline",
                            dependency_type="trigger_pipeline",
                        )
                    )

    def find_missing_dependencies(self) -> None:
        """Identify dependencies that reference nonexistent assets."""
        # Dataset → LinkedService
        for ds in self.inventory.datasets:
            ref = ds.properties.linked_service_name
            if ref and ref.reference_name not in self._ls_names:
                self._missing.append(
                    MissingDependency(
                        source_asset=ds.name,
                        source_type="dataset",
                        missing_reference=ref.reference_name,
                        expected_type="linked_service",
                        dependency_type="dataset_linked_service",
                    )
                )
            # Also add as dependency edge even if it exists
            if ref:
                self._dependencies.append(
                    DependencyEdge(
                        source=ds.name,
                        target=ref.reference_name,
                        source_type="dataset",
                        target_type="linked_service",
                        dependency_type="dataset_linked_service",
                    )
                )

        # Check all dependency edges for missing targets
        known_names = self._ls_names | self._ds_names | self._df_names | self._pl_names
        known_names |= {a.asset_name for a in self._assets}
        known_names |= {c.component_name for c in self._components}
        for dep in self._dependencies:
            if dep.target not in known_names:
                # Check if already reported
                already = any(
                    m.source_asset == dep.source
                    and m.missing_reference == dep.target
                    for m in self._missing
                )
                if not already:
                    self._missing.append(
                        MissingDependency(
                            source_asset=dep.source,
                            source_type=dep.source_type,
                            missing_reference=dep.target,
                            expected_type=dep.target_type,
                            dependency_type=dep.dependency_type,
                        )
                    )

    def create_summary(self) -> DiscoverySummary:
        """Build aggregate counts."""
        inv = self.inventory
        src_count = sum(
            len(df.properties.type_properties.sources)
            for df in inv.data_flows
        )
        sink_count = sum(
            len(df.properties.type_properties.sinks)
            for df in inv.data_flows
        )
        xform_count = sum(
            len(df.properties.type_properties.transformations)
            for df in inv.data_flows
        )
        act_count = sum(
            1 for component in self._components
            if component.component_type == "activity"
        )

        return DiscoverySummary(
            pipeline_count=len(inv.pipelines),
            activity_count=act_count,
            data_flow_count=len(inv.data_flows),
            source_count=src_count,
            sink_count=sink_count,
            transformation_count=xform_count,
            dataset_count=len(inv.datasets),
            linked_service_count=len(inv.linked_services),
            trigger_count=len(inv.triggers),
            dependency_count=len(self._dependencies),
            missing_dependency_count=len(self._missing),
            artifact_count=sum(1 for a in self._assets if not a.is_component),
            component_count=len(self._components),
            expression_count=len(self._expressions),
            connection_reference_count=len(self._connection_references),
        )

    def _raw_definition(self, category: str, name: str, model) -> dict[str, Any]:
        for definition in self.inventory.source_definitions.get(category, []):
            if definition.get("name") == name:
                return definition
        return model.model_dump(by_alias=True, exclude_none=True)

    @staticmethod
    def _transformation_statements(script_lines: list[str]) -> dict[str, str]:
        script = "\n".join(script_lines)
        return {
            match.group(2): match.group(1).strip()
            for match in _STATEMENT_RE.finditer(script)
        }

    def _extract_expressions(
        self,
        value: Any,
        source_reference: str,
        property_path: str,
        register: bool = False,
    ) -> list[SourceExpression]:
        found: list[SourceExpression] = []

        def walk(item: Any, path: str) -> None:
            if isinstance(item, dict):
                if item.get("type") == "Expression" and "value" in item:
                    found.append(
                        SourceExpression(
                            source_reference=source_reference,
                            property_path=path,
                            value=item["value"],
                            expression_type="Expression",
                        )
                    )
                    return
                for key, child in item.items():
                    walk(child, f"{path}.{key}" if path else key)
            elif isinstance(item, list):
                for index, child in enumerate(item):
                    walk(child, f"{path}[{index}]")
            elif isinstance(item, str) and item.startswith("@"):
                found.append(
                    SourceExpression(
                        source_reference=source_reference,
                        property_path=path,
                        value=item,
                        expression_type="ADFExpressionString",
                    )
                )

        walk(value, property_path)
        if register:
            self._expressions.extend(found)
        return found

    @staticmethod
    def _extract_connection_references(
        value: Any, source_reference: str
    ) -> list[ConnectionReference]:
        found: list[ConnectionReference] = []

        def walk(item: Any, path: str) -> None:
            if isinstance(item, dict):
                ref_type = str(item.get("type", ""))
                if (
                    "referenceName" in item
                    and "LinkedService" in ref_type
                ):
                    found.append(
                        ConnectionReference(
                            source_reference=source_reference,
                            connection_name=item["referenceName"],
                            reference_type=ref_type,
                            property_path=path,
                        )
                    )
                for key, child in item.items():
                    walk(child, f"{path}.{key}" if path else key)
            elif isinstance(item, list):
                for index, child in enumerate(item):
                    walk(child, f"{path}[{index}]")

        walk(value, "")
        return found
