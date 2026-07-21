"""Deployment API routes and deployment page — Phase 7.

Runs a dry-run or mock deployment of an approved plan and serves a
minimal deployment page. No real Fabric calls; REAL mode returns 501.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from src.migration.deployment import DeploymentService, RealModeNotImplementedError
from src.migration.deployment_store import get_deployment, get_latest_deployment
from src.models.schemas import DeploymentMode, DeploymentStatus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["deployments"])

FRONTEND_ROOT = Path(__file__).resolve().parent.parent.parent / "frontend"


class DeploymentStartBody(BaseModel):
    plan_id: int
    approval_id: int
    mode: str


@router.post("/api/deployments/start")
async def start_deployment(body: DeploymentStartBody):
    """Start a dry-run or mock deployment of an approved plan."""
    try:
        mode = DeploymentMode(body.mode)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode '{body.mode}'. Use DRY_RUN or MOCK.",
        )

    try:
        result = DeploymentService().deploy(body.plan_id, body.approval_id, mode)
    except RealModeNotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc))

    payload = result.model_dump(mode="json")
    if result.status == DeploymentStatus.BLOCKED:
        # Authorization failed — surface the guard error as 409.
        return JSONResponse(status_code=409, content=payload)
    return payload


@router.get("/api/deployments/latest")
async def latest_deployment():
    """Return the most recent deployment run."""
    record = get_latest_deployment()
    if record is None:
        raise HTTPException(
            status_code=404, detail="No deployment has been run yet."
        )
    return _serialize(record)


@router.get("/api/deployments/{deployment_id}")
async def deployment_by_id(deployment_id: int):
    """Return a specific deployment run by id."""
    record = get_deployment(deployment_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"Deployment {deployment_id} not found."
        )
    return _serialize(record)


def _serialize(record: dict) -> dict:
    return {
        "deployment_id": record["id"],
        "plan_id": record["plan_id"],
        "approval_id": record["approval_id"],
        "mode": record["mode"],
        "status": record["status"],
        "created_at": record["created_at"],
        "completed_at": record["completed_at"],
        "result": record["result"].model_dump(mode="json"),
    }


# ── Deployment page (static) ─────────────────────────────────────


@router.get("/deployment", include_in_schema=False)
async def deployment_page():
    return FileResponse(FRONTEND_ROOT / "deployment.html", media_type="text/html")


@router.get("/deployment.js", include_in_schema=False)
async def deployment_js():
    return FileResponse(
        FRONTEND_ROOT / "deployment.js", media_type="text/javascript"
    )
