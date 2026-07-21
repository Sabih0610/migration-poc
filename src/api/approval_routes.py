"""Approval API routes and approval page — Phase 6.

Exposes the plan-approval lifecycle over HTTP and serves a minimal
static approval page. No deployment is performed anywhere here.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.approvals import approval_service as svc
from src.approvals.approval_service import ApprovalError
from src.approvals.approval_store import get_latest_for_plan
from src.migration.plan_store import get_plan

logger = logging.getLogger(__name__)

router = APIRouter(tags=["approvals"])

FRONTEND_ROOT = Path(__file__).resolve().parent.parent.parent / "frontend"

# Error code -> HTTP status.
_STATUS_MAP = {
    "PLAN_NOT_FOUND": 404,
    "APPROVAL_NOT_FOUND": 404,
    "NOT_EXECUTABLE": 409,
    "INVALID_TRANSITION": 409,
    "INVALIDATED": 409,
}


class ApprovalActionBody(BaseModel):
    """Request body carrying the acting user and an optional comment."""

    user: str
    comment: str = ""


def _raise_http(exc: ApprovalError) -> None:
    raise HTTPException(status_code=_STATUS_MAP.get(exc.code, 400), detail=exc.message)


def _require_user(body: "ApprovalActionBody") -> None:
    """Reject blank / whitespace-only user names (400)."""
    if not body.user or not body.user.strip():
        raise HTTPException(status_code=400, detail="A non-blank user is required.")


# ── API ──────────────────────────────────────────────────────────


@router.post("/api/plans/{plan_id}/request-approval")
async def request_approval(plan_id: int, body: ApprovalActionBody):
    """Request approval for a plan."""
    _require_user(body)
    try:
        result = svc.request_approval(plan_id, body.user, body.comment)
    except ApprovalError as exc:
        _raise_http(exc)
    return result.model_dump(mode="json")


@router.post("/api/approvals/{approval_id}/approve")
async def approve(approval_id: int, body: ApprovalActionBody):
    """Approve a pending request."""
    _require_user(body)
    try:
        result = svc.approve(approval_id, body.user, body.comment)
    except ApprovalError as exc:
        _raise_http(exc)
    return result.model_dump(mode="json")


@router.post("/api/approvals/{approval_id}/reject")
async def reject(approval_id: int, body: ApprovalActionBody):
    """Reject a pending request."""
    _require_user(body)
    try:
        result = svc.reject(approval_id, body.user, body.comment)
    except ApprovalError as exc:
        _raise_http(exc)
    return result.model_dump(mode="json")


@router.get("/api/approvals/{approval_id}")
async def get_approval(approval_id: int):
    """Return a single approval's state."""
    try:
        result = svc.get_status(approval_id)
    except ApprovalError as exc:
        _raise_http(exc)
    return result.model_dump(mode="json")


@router.get("/api/plans/{plan_id}/approval-status")
async def approval_status(plan_id: int):
    """Return the latest approval for a plan and whether it can deploy."""
    if get_plan(plan_id) is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found.")

    latest = get_latest_for_plan(plan_id)
    if latest is None:
        return {"plan_id": plan_id, "status": "NONE", "approval": None,
                "can_deploy": False}

    return {
        "plan_id": plan_id,
        "status": latest.status.value,
        "approval": latest.model_dump(mode="json"),
        "can_deploy": svc.can_deploy(plan_id, latest.approval_id),
    }


# ── Approval page (static) ───────────────────────────────────────


@router.get("/approval", include_in_schema=False)
async def approval_page():
    return FileResponse(FRONTEND_ROOT / "approval.html", media_type="text/html")


@router.get("/approval.js", include_in_schema=False)
async def approval_js():
    return FileResponse(
        FRONTEND_ROOT / "approval.js", media_type="text/javascript"
    )


@router.get("/styles.css", include_in_schema=False)
async def approval_css():
    return FileResponse(FRONTEND_ROOT / "styles.css", media_type="text/css")


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # No favicon asset; return an empty 204 to avoid noisy 404s.
    return Response(status_code=204)
