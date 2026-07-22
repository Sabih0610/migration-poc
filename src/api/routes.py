"""Discovery API routes — Phase 3.

Endpoints for scanning mock ADF fixtures and retrieving persisted
source-definition discovery snapshots.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from src.connectors.azure_adf_client import AzureDiscoveryError
from src.migration.discovery_runner import run_discovery
from src.migration.discovery_store import (
    get_discovery,
    get_latest_discovery as load_latest_discovery,
)
from src.models.schemas import ADFInventory, DiscoveryResult

# Azure discovery error code -> HTTP status (sanitized).
_AZURE_STATUS = {
    "AZURE_DISCOVERY_DISABLED": 409,
    "AZURE_CONFIG_INCOMPLETE": 409,
    "AZURE_AUTHORIZATION_FAILED": 403,
    "AZURE_NOT_FOUND": 404,
    "AZURE_TIMEOUT": 504,
}

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/discovery", tags=["discovery"])

FIXTURES_ROOT = Path(__file__).resolve().parent.parent.parent / "fixtures"


def get_latest_discovery() -> DiscoveryResult | None:
    """Return the latest persisted result (or None if no scan yet)."""
    record = load_latest_discovery()
    return record["result"] if record else None


def get_latest_discovery_record() -> dict | None:
    """Return the latest persisted discovery record."""
    return load_latest_discovery()


def get_latest_inventory() -> ADFInventory | None:
    """Return the inventory embedded in the latest persisted snapshot."""
    result = get_latest_discovery()
    return result.inventory if result else None


def _require_scan() -> DiscoveryResult:
    """Return latest result or raise 404."""
    result = get_latest_discovery()
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="No discovery scan has been run yet. POST /api/discovery/scan first.",
        )
    return result


@router.post("/scan")
async def scan(mode: str = "fixture"):
    """Run a discovery scan.

    mode='fixture' (default) scans local fixtures; mode='azure' runs the
    read-only Azure Data Factory connector. Azure is only available when
    discovery is enabled and fully configured (otherwise 409).
    """
    try:
        record = run_discovery(mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except AzureDiscoveryError as exc:
        raise HTTPException(
            status_code=_AZURE_STATUS.get(exc.code, 502),
            detail={"code": exc.code, "message": exc.message},
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "status": "completed",
        "mode": mode,
        "discovery_id": record["id"],
        "summary": record["result"].summary.model_dump(),
    }


@router.get("/latest")
async def latest_discovery():
    """Return metadata and the complete latest discovery snapshot."""
    record = get_latest_discovery_record()
    if record is None:
        raise HTTPException(status_code=404, detail="No discovery scan found.")
    return _serialize_record(record)


@router.get("/runs/{discovery_id}")
async def discovery_by_id(discovery_id: int):
    record = get_discovery(discovery_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"Discovery {discovery_id} not found."
        )
    return _serialize_record(record)


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


def _serialize_record(record: dict) -> dict:
    return {
        "discovery_id": record["id"],
        "artifact_count": record["artifact_count"],
        "component_count": record["component_count"],
        "created_at": record["created_at"],
        "result": record["result"].model_dump(mode="json", by_alias=True),
    }
