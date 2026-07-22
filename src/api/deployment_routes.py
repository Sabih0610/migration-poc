"""Deployment API routes and deployment page — Phase 7 / Phase 10.

Runs DRY_RUN, MOCK, or REAL deployment of an approved plan and serves a
minimal deployment page. REAL only runs when Fabric deployment is
explicitly enabled and configured (otherwise 409).
"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from src.config import get_settings
from src.connectors.fabric_client import (
    build_fabric_client_from_settings, FabricError,
)
from src.migration.deployment import (
    DeploymentService,
    FabricDeploymentDisabledError,
)
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
    """Start a DRY_RUN, MOCK, or REAL deployment of an approved plan."""
    try:
        mode = DeploymentMode(body.mode)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode '{body.mode}'. Use DRY_RUN, MOCK, or REAL.",
        )

    try:
        result = DeploymentService().deploy(body.plan_id, body.approval_id, mode)
    except FabricDeploymentDisabledError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except FabricError as exc:
        raise HTTPException(
            status_code=502, detail={"code": exc.code, "message": exc.message}
        )

    payload = result.model_dump(mode="json")
    if result.status == DeploymentStatus.BLOCKED:
        # Authorization / package verification failed — surface as 409.
        return JSONResponse(status_code=409, content=payload)
    return payload


@router.get("/api/deployments/fabric-readiness")
async def fabric_readiness():
    """Report Fabric deployment readiness (never contacts Fabric)."""
    settings = get_settings()
    missing = settings.get_missing_fabric_settings()
    return {
        "enabled": settings.fabric_deployment_enabled,
        "configured": not missing,
        "ready": settings.fabric_deployment_ready(),
        "missing_settings": missing,
        "workspace_id": settings.fabric_workspace_id or None,
        "capacity_id": settings.fabric_capacity_id or None,
    }


@router.post("/api/deployments/fabric-verify")
async def fabric_verify():
    """Read-only verification of the configured Fabric environment."""
    settings = get_settings()
    try:
        client = build_fabric_client_from_settings(settings)
        env = client.verify_environment()
    except FabricError as exc:
        code_map = {
            "FABRIC_DEPLOYMENT_DISABLED": 409, "FABRIC_CONFIG_INCOMPLETE": 409,
            "FABRIC_AUTHORIZATION_FAILED": 403, "FABRIC_NOT_FOUND": 404,
            "FABRIC_TIMEOUT": 504,
        }
        raise HTTPException(
            status_code=code_map.get(exc.code, 502),
            detail={"code": exc.code, "message": exc.message},
        )
    return env


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
