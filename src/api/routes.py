"""Discovery API routes — Phase 3.

Endpoints for scanning mock ADF fixtures and retrieving
discovery results. Stores latest result in memory for POC.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from src.fixtures_loader import load_mock_adf_inventory
from src.migration.dependency_graph import DependencyGraph
from src.migration.discovery import ADFDiscoveryService
from src.models.schemas import ADFInventory, DiscoveryResult

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/discovery", tags=["discovery"])

# In-memory storage for POC
_latest_result: DiscoveryResult | None = None
_latest_graph: DependencyGraph | None = None
_latest_inventory: ADFInventory | None = None

FIXTURES_ROOT = Path(__file__).resolve().parent.parent.parent / "fixtures"


def get_latest_discovery() -> DiscoveryResult | None:
    """Return the latest discovery result (or None if no scan yet)."""
    return _latest_result


def get_latest_inventory() -> ADFInventory | None:
    """Return the inventory from the latest scan (or None if no scan yet)."""
    return _latest_inventory


def _require_scan() -> DiscoveryResult:
    """Return latest result or raise 404."""
    if _latest_result is None:
        raise HTTPException(
            status_code=404,
            detail="No discovery scan has been run yet. POST /api/discovery/scan first.",
        )
    return _latest_result


@router.post("/scan")
async def scan():
    """Run discovery scan against mock fixtures."""
    global _latest_result, _latest_graph, _latest_inventory

    try:
        inventory = load_mock_adf_inventory(FIXTURES_ROOT)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    _latest_inventory = inventory
    service = ADFDiscoveryService(inventory)
    _latest_result = service.scan_inventory()

    graph = DependencyGraph()
    graph.build_graph(_latest_result)
    _latest_graph = graph

    return {
        "status": "completed",
        "summary": _latest_result.summary.model_dump(),
    }


@router.get("/assets")
async def get_assets():
    """Return all discovered assets."""
    result = _require_scan()
    return {
        "count": len(result.assets),
        "assets": [a.model_dump() for a in result.assets],
    }


@router.get("/dependencies")
async def get_dependencies():
    """Return all dependency edges and missing references."""
    result = _require_scan()
    return {
        "dependency_count": len(result.dependencies),
        "dependencies": [d.model_dump() for d in result.dependencies],
        "missing_count": len(result.missing_dependencies),
        "missing_dependencies": [m.model_dump() for m in result.missing_dependencies],
    }


@router.get("/summary")
async def get_summary():
    """Return discovery summary counts."""
    result = _require_scan()
    return result.summary.model_dump()
