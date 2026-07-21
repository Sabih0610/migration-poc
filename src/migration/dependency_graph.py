"""Dependency graph using networkx — Phase 3.

Builds a directed graph from discovery results and provides
upstream/downstream traversal, execution ordering, and cycle detection.

Missing references are added as nodes but do not crash the graph.
"""

import json
import logging
from typing import Any

import networkx as nx

from src.models.schemas import DependencyEdge, DiscoveryResult

logger = logging.getLogger(__name__)


class DependencyGraph:
    """Directed dependency graph for ADF assets."""

    def __init__(self):
        self._graph: nx.DiGraph = nx.DiGraph()

    @property
    def graph(self) -> nx.DiGraph:
        return self._graph

    def build_graph(self, result: DiscoveryResult) -> nx.DiGraph:
        """Build graph from discovery result edges.

        Each node gets an 'asset_type' attribute.
        Each edge gets a 'dependency_type' attribute.
        Missing references are added as nodes with type 'missing'.
        """
        self._graph = nx.DiGraph()

        # Add all discovered assets as nodes
        for asset in result.assets:
            self._graph.add_node(
                asset.asset_name,
                asset_type=asset.asset_type,
                parent=asset.parent,
            )

        # Add dependency edges
        for dep in result.dependencies:
            # Ensure both nodes exist
            if dep.source not in self._graph:
                self._graph.add_node(
                    dep.source, asset_type=dep.source_type
                )
            if dep.target not in self._graph:
                self._graph.add_node(
                    dep.target, asset_type=dep.target_type
                )
            self._graph.add_edge(
                dep.source,
                dep.target,
                dependency_type=dep.dependency_type,
                source_type=dep.source_type,
                target_type=dep.target_type,
            )

        # Mark missing nodes
        for missing in result.missing_dependencies:
            if missing.missing_reference in self._graph:
                self._graph.nodes[missing.missing_reference][
                    "is_missing"
                ] = True

        logger.info(
            "Graph built: %d nodes, %d edges.",
            self._graph.number_of_nodes(),
            self._graph.number_of_edges(),
        )
        return self._graph

    def get_upstream(self, node: str) -> list[str]:
        """Get all nodes that the given node depends on."""
        if node not in self._graph:
            return []
        return list(nx.descendants(self._graph, node))

    def get_downstream(self, node: str) -> list[str]:
        """Get all nodes that depend on the given node."""
        if node not in self._graph:
            return []
        return list(nx.ancestors(self._graph, node))

    def get_execution_order(self) -> list[str]:
        """Return topological sort (execution order).

        Returns empty list if the graph has cycles.
        """
        try:
            return list(nx.topological_sort(self._graph))
        except nx.NetworkXUnfeasible:
            logger.warning("Cannot determine execution order: graph has cycles.")
            return []

    def detect_cycles(self) -> list[list[str]]:
        """Return list of cycles in the graph."""
        try:
            cycles = list(nx.simple_cycles(self._graph))
            if cycles:
                logger.warning("Cycles detected: %d", len(cycles))
            return cycles
        except Exception as exc:
            logger.error("Cycle detection failed: %s", exc)
            return []

    def export_graph(self) -> dict[str, Any]:
        """Export graph as a JSON-serializable dict."""
        nodes = []
        for name, data in self._graph.nodes(data=True):
            nodes.append({
                "name": name,
                "asset_type": data.get("asset_type", "unknown"),
                "parent": data.get("parent"),
                "is_missing": data.get("is_missing", False),
            })

        edges = []
        for source, target, data in self._graph.edges(data=True):
            edges.append({
                "source": source,
                "target": target,
                "dependency_type": data.get("dependency_type", "unknown"),
            })

        return {
            "node_count": self._graph.number_of_nodes(),
            "edge_count": self._graph.number_of_edges(),
            "nodes": nodes,
            "edges": edges,
            "has_cycles": len(self.detect_cycles()) > 0,
        }
