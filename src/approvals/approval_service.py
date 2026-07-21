"""Approval service — Phase 6.

Business logic for the plan-approval lifecycle. Approvals are bound to a
plan's id, version, and content fingerprint; any change to the plan (or a
newer plan version for the same assessment) invalidates prior approvals.

No deletes. No Fabric calls. No deployment — deployment authorization is
delegated to deployment_guard.
"""

import logging
from typing import Optional

from src.approvals import approval_store
from src.migration.plan_store import compute_plan_fingerprint, get_plan, list_plans
from src.models.schemas import ApprovalResult, ApprovalStatus

logger = logging.getLogger(__name__)


class ApprovalError(Exception):
    """Raised for invalid approval operations."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code


# ── Requesting ───────────────────────────────────────────────────


def request_approval(
    plan_id: int, user: str, comment: str = ""
) -> ApprovalResult:
    """Create a PENDING approval request for an executable plan."""
    record = get_plan(plan_id)
    if record is None:
        raise ApprovalError(f"Plan {plan_id} does not exist.", "PLAN_NOT_FOUND")
    plan = record["plan"]
    if not plan.executable:
        raise ApprovalError(
            f"Plan {plan_id} is not executable and cannot be approved.",
            "NOT_EXECUTABLE",
        )

    # A newer version supersedes older approvals for the same assessment.
    invalidate_stale_approvals(plan_id)

    fingerprint = compute_plan_fingerprint(plan)
    return approval_store.save_approval(
        plan_id=plan_id,
        plan_version=record["version"],
        plan_fingerprint=fingerprint,
        requested_by=user,
        request_comment=comment,
    )


# ── Deciding ─────────────────────────────────────────────────────


def approve(approval_id: int, user: str, comment: str = "") -> ApprovalResult:
    """Approve a PENDING request."""
    return _decide(approval_id, ApprovalStatus.APPROVED, user, comment)


def reject(approval_id: int, user: str, comment: str = "") -> ApprovalResult:
    """Reject a PENDING request."""
    return _decide(approval_id, ApprovalStatus.REJECTED, user, comment)


def _decide(
    approval_id: int, new_status: ApprovalStatus, user: str, comment: str
) -> ApprovalResult:
    approval = approval_store.get_approval(approval_id)
    if approval is None:
        raise ApprovalError(
            f"Approval {approval_id} does not exist.", "APPROVAL_NOT_FOUND"
        )
    if ApprovalStatus(approval.status) != ApprovalStatus.PENDING:
        raise ApprovalError(
            f"Approval {approval_id} is {approval.status}; only PENDING "
            "approvals can be decided.",
            "INVALID_TRANSITION",
        )

    # If the plan changed since the request, invalidate instead of deciding.
    record = get_plan(approval.plan_id)
    if record is None:
        raise ApprovalError(
            f"Plan {approval.plan_id} no longer exists.", "PLAN_NOT_FOUND"
        )
    if compute_plan_fingerprint(record["plan"]) != approval.plan_fingerprint:
        approval_store.update_status(
            approval_id,
            ApprovalStatus.INVALIDATED,
            decided_by="system",
            decision_comment="Plan changed before decision.",
        )
        raise ApprovalError(
            "Plan changed since approval was requested; request invalidated.",
            "INVALIDATED",
        )

    return approval_store.update_status(
        approval_id, new_status, decided_by=user, decision_comment=comment
    )


# ── Invalidation ─────────────────────────────────────────────────


def invalidate_stale_approvals(plan_id: int) -> list[int]:
    """Invalidate approvals superseded by a newer plan version or a
    fingerprint change. Returns the ids that were invalidated."""
    record = get_plan(plan_id)
    if record is None:
        return []
    assessment_id = record["assessment_id"]
    versions = [
        p["version"] for p in list_plans() if p["assessment_id"] == assessment_id
    ]
    latest_version = max(versions) if versions else record["version"]

    invalidated: list[int] = []
    for approval in approval_store.list_approvals():
        if ApprovalStatus(approval.status) not in (
            ApprovalStatus.PENDING,
            ApprovalStatus.APPROVED,
        ):
            continue
        appr_plan = get_plan(approval.plan_id)
        if appr_plan is None or appr_plan["assessment_id"] != assessment_id:
            continue

        superseded = approval.plan_version < latest_version
        changed = (
            compute_plan_fingerprint(appr_plan["plan"])
            != approval.plan_fingerprint
        )
        if superseded or changed:
            approval_store.update_status(
                approval.approval_id,
                ApprovalStatus.INVALIDATED,
                decided_by="system",
                decision_comment="Superseded by a newer plan version.",
            )
            invalidated.append(approval.approval_id)

    if invalidated:
        logger.info("Invalidated stale approvals: %s", invalidated)
    return invalidated


# ── Queries ──────────────────────────────────────────────────────


def can_deploy(plan_id: int, approval_id: int) -> bool:
    """True if the approval authorizes deploying the plan."""
    # Imported lazily to avoid a module-level import cycle.
    from src.approvals.deployment_guard import (
        DeploymentAuthorizationError,
        validate_deployment_authorization,
    )

    try:
        validate_deployment_authorization(plan_id, approval_id)
        return True
    except DeploymentAuthorizationError:
        return False


def get_status(approval_id: int) -> ApprovalResult:
    """Return the current state of an approval."""
    approval = approval_store.get_approval(approval_id)
    if approval is None:
        raise ApprovalError(
            f"Approval {approval_id} does not exist.", "APPROVAL_NOT_FOUND"
        )
    return approval
