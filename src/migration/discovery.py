"""ADF Discovery Service — Phase 3.

Scans an ADFInventory and produces a DiscoveryResult containing
all discovered assets, dependency edges, and missing references.

Uses mock fixtures only. No Azure calls. No assessment logic.
"""

import json
import logging
from typing import Optional

from src.models.schemas import (
    ADFInventory,
    DependencyEdge,
    DiscoveredAsset,
    DiscoveryResult,
    DiscoverySummary,
    MissingDependency,
)

logger = logging.getLogger(__name__)


class ADFDiscoveryService:
    """Discovers assets and dependencies from an ADF inventory."""

    def __init__(self, inventory: ADFInventory):
        self.inventory = inventory
        self._assets: list[DiscoveredAsset] = []
        self._dependencies: list[DependencyEdge] = []
        self._missing: list[MissingDependency] = []

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

        self.discover_assets()
        self.discover_pipeline_dependencies()
        self.discover_dataflow_dependencies()
        self.discover_trigger_dependencies()
        self.find_missing_dependencies()

        summary = self.create_summary()

        result = DiscoveryResult(
            assets=list(self._assets),
            dependencies=list(self._dependencies),
            missing_dependencies=list(self._missing),
            summary=summary,
        )

        logger.info(
            "Discovery complete: %d assets, %d dependencies, %d missing.",
            len(self._assets),
            len(self._dependencies),
            len(self._missing),
        )

        return result

    def discover_assets(self) -> None:
        """Discover all asset types from the inventory."""
        # Linked services
        for ls in self.inventory.linked_services:
            self._assets.append(
                DiscoveredAsset(
                    asset_type="linked_service",
                    asset_name=ls.name,
                    metadata={"service_type": ls.properties.type},
                )
            )

        # Datasets
        for ds in self.inventory.datasets:
            self._assets.append(
                DiscoveredAsset(
                    asset_type="dataset",
                    asset_name=ds.name,
                    metadata={"dataset_type": ds.properties.type},
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
                    },
                )
            )
            for act in pl.properties.activities:
                self._assets.append(
                    DiscoveredAsset(
                        asset_type="activity",
                        asset_name=act.name,
                        parent=pl.name,
                        metadata={"activity_type": act.type},
                    )
                )

        # Triggers
        for trg in self.inventory.triggers:
            self._assets.append(
                DiscoveredAsset(
                    asset_type="trigger",
                    asset_name=trg.name,
                    metadata={"trigger_type": trg.properties.type},
                )
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
                # Parse nested activities as PipelineActivity
                from src.models.schemas import PipelineActivity

                for nested_list in [true_acts, false_acts]:
                    parsed = []
                    for nested in nested_list:
                        try:
                            parsed.append(PipelineActivity(**nested))
                        except Exception:
                            pass
                    if parsed:
                        # Also register nested activities as assets
                        for nested_act in parsed:
                            self._assets.append(
                                DiscoveredAsset(
                                    asset_type="activity",
                                    asset_name=nested_act.name,
                                    parent=pipeline_name,
                                    metadata={"activity_type": nested_act.type},
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
            len(pl.properties.activities) for pl in inv.pipelines
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
        )
