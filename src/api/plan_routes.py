"""Migration plan API routes — Phase 5.

Generates a Fabric migration plan from the latest discovery + assessment
and persists it. Both discovery and assessment must exist first (409
otherwise). Plans are stored in SQLite and never contain secrets.
No deployment is performed.
"""

import logging

from fastapi import APIRouter, HTTPException

from src.api.routes import get_latest_discovery, get_latest_inventory
from src.migration.assessment_store import get_latest_assessment
from src.migration.plan_store import get_latest_plan, get_plan, save_plan
from src.migration.planner import MigrationPlanner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plans", tags=["plans"])


@router.post("/generate")
async def generate_plan():
    """Generate and persist a migration plan from discovery + assessment."""
    discovery = get_latest_discovery()
    inventory = get_latest_inventory()
    if discovery is None or inventory is None:
        raise HTTPException(
            status_code=409,
            detail="No discovery scan found. POST /api/discovery/scan first.",
        )

    assessment_record = get_latest_assessment()
    if assessment_record is None:
        raise HTTPException(
            status_code=409,
            detail="No assessment found. POST /api/assessment/run first.",
        )

    plan = MigrationPlanner(inventory).generate_plan(
        discovery, assessment_record["result"]
    )
    record = save_plan(plan, assessment_id=assessment_record["id"])

    # A new plan version supersedes any prior approvals for this assessment.
    try:
        from src.approvals.approval_service import invalidate_stale_approvals

        invalidate_stale_approvals(record["id"])
    except Exception as exc:  # approvals are non-critical to plan generation
        logger.warning("Could not invalidate stale approvals: %s", exc)

    return {
        "status": "completed",
        "plan_id": record["id"],
        "version": record["version"],
        "executable": record["executable"],
        "overall_risk": record["overall_risk"],
        "summary": plan.summary.model_dump(mode="json"),
    }


@router.get("/latest")
async def latest_plan():
    """Return the most recent migration plan."""
    record = get_latest_plan()
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No plan has been generated yet. POST /api/plans/generate first.",
        )
    return _serialize(record)


@router.get("/{plan_id}")
async def plan_by_id(plan_id: int):
    """Return a specific migration plan by id."""
    record = get_plan(plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found.")
    return _serialize(record)


def _serialize(record: dict) -> dict:
    """Shape a store record into a JSON response."""
    return {
        "plan_id": record["id"],
        "assessment_id": record["assessment_id"],
        "version": record["version"],
        "executable": record["executable"],
        "overall_risk": record["overall_risk"],
        "created_at": record["created_at"],
        "plan": record["plan"].model_dump(mode="json"),
    }
