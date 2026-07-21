"""Assessment API routes — Phase 4.

Runs the compatibility assessment against the latest discovery result
and persists it. Discovery must run first (409 otherwise). Results are
stored in SQLite and never contain secrets.
"""

import logging

from fastapi import APIRouter, HTTPException

from src.api.routes import get_latest_discovery, get_latest_inventory
from src.migration.assessment import ADFCompatibilityAssessment
from src.migration.assessment_store import (
    get_assessment,
    get_latest_assessment,
    save_assessment,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/assessment", tags=["assessment"])


@router.post("/run")
async def run_assessment():
    """Assess the latest discovery result and persist it."""
    discovery = get_latest_discovery()
    inventory = get_latest_inventory()
    if discovery is None or inventory is None:
        raise HTTPException(
            status_code=409,
            detail="No discovery scan found. POST /api/discovery/scan first.",
        )

    engine = ADFCompatibilityAssessment(inventory)
    result = engine.assess_discovery(discovery)
    assessment_id = save_assessment(result)

    return {
        "status": "completed",
        "assessment_id": assessment_id,
        "overall_status": result.overall_status.value,
        "summary": result.summary.model_dump(mode="json"),
    }


@router.get("/latest")
async def latest_assessment():
    """Return the most recent assessment run."""
    record = get_latest_assessment()
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No assessment has been run yet. POST /api/assessment/run first.",
        )
    return _serialize(record)


@router.get("/{assessment_id}")
async def assessment_by_id(assessment_id: int):
    """Return a specific assessment run by id."""
    record = get_assessment(assessment_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"Assessment {assessment_id} not found."
        )
    return _serialize(record)


def _serialize(record: dict) -> dict:
    """Shape a store record into a JSON response."""
    return {
        "assessment_id": record["id"],
        "created_at": record["created_at"],
        "overall_status": record["overall_status"],
        "result": record["result"].model_dump(mode="json"),
    }
