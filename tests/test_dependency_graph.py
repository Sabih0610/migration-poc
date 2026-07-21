"""Tests for the dependency graph — Phase 3."""

import pytest
from pathlib import Path

from src.fixtures_loader import load_mock_adf_inventory
from src.migration.dependency_graph import DependencyGraph
from src.migration.discovery import ADFDiscoveryService
from src.models.schemas import DependencyEdge, DiscoveryResult

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def discovery_result():
    inv = load_mock_adf_inventory(FIXTURES)
    svc = ADFDiscoveryService(inv)
    return svc.scan_inventory()


def test_build_graph(discovery_result):
    graph = DependencyGraph()
    g = graph.build_graph(discovery_result)
    assert g.number_of_nodes() > 0
    assert g.number_of_edges() > 0


def test_get_upstream(discovery_result):
    graph = DependencyGraph()
    graph.build_graph(discovery_result)
    # The pipeline should depend on the data flow
    upstream = graph.get_upstream("pl_sales_processing_legacy")
    assert "df_sales_processing_legacy" in upstream


def test_get_downstream(discovery_result):
    graph = DependencyGraph()
    graph.build_graph(discovery_result)
    # df_sales_processing_legacy should have downstream pl_sales_processing_legacy
    downstream = graph.get_downstream("df_sales_processing_legacy")
    assert "pl_sales_processing_legacy" in downstream


def test_detect_cycles():
    graph = DependencyGraph()
    res = DiscoveryResult(
        assets=[],
        dependencies=[
            DependencyEdge(
                source="A",
                target="B",
                source_type="t",
                target_type="t",
                dependency_type="t",
            ),
            DependencyEdge(
                source="B",
                target="A",
                source_type="t",
                target_type="t",
                dependency_type="t",
            ),
        ],
    )
    graph.build_graph(res)
    cycles = graph.detect_cycles()
    assert len(cycles) > 0


def test_execution_order(discovery_result):
    graph = DependencyGraph()
    graph.build_graph(discovery_result)
    order = graph.get_execution_order()
    assert len(order) > 0
