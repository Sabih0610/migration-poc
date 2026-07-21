"""Approval persistence — Phase 6.

Saves and loads approval requests to the approval_requests table.
Stores no secrets — only plan identity, fingerprint, user names,
comments, and timestamps.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from src.database import ApprovalRequestRecord, get_session_factory
from src.models.schemas import ApprovalResult, ApprovalStatus, ApprovalSummary

logger = logging.getLogger(__name__)


def _to_result(record: ApprovalRequestRecord) -> ApprovalResult:
    """Convert an ORM row into an ApprovalResult."""
    return ApprovalResult(
        approval_id=record.id,
        plan_id=record.plan_id,
        plan_version=record.plan_version,
        plan_fingerprint=record.plan_fingerprint,
        status=ApprovalStatus(record.status),
        requested_by=record.requested_by,
        decided_by=record.decided_by,
        request_comment=record.request_comment or "",
        decision_comment=record.decision_comment or "",
        request_time=record.created_at.isoformat() if record.created_at else None,
        decision_time=record.decided_at.isoformat() if record.decided_at else None,
    )


def save_approval(
    plan_id: int,
    plan_version: int,
    plan_fingerprint: str,
    requested_by: str,
    request_comment: str = "",
) -> ApprovalResult:
    """Create a new PENDING approval request and return it."""
    session = get_session_factory()()
    try:
        record = ApprovalRequestRecord(
            plan_id=plan_id,
            plan_version=plan_version,
            plan_fingerprint=plan_fingerprint,
            status=ApprovalStatus.PENDING.value,
            requested_by=requested_by,
            request_comment=request_comment,
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        logger.info(
            "Approval requested id=%d for plan=%d v%d.",
            record.id, plan_id, plan_version,
        )
        return _to_result(record)
    finally:
        session.close()


def get_approval(approval_id: int) -> Optional[ApprovalResult]:
    """Load a single approval by id, or None if not found."""
    session = get_session_factory()()
    try:
        record = session.get(ApprovalRequestRecord, approval_id)
        return _to_result(record) if record else None
    finally:
        session.close()


def get_latest_for_plan(plan_id: int) -> Optional[ApprovalResult]:
    """Load the most recent approval for a plan, or None."""
    session = get_session_factory()()
    try:
        record = (
            session.query(ApprovalRequestRecord)
            .filter(ApprovalRequestRecord.plan_id == plan_id)
            .order_by(ApprovalRequestRecord.id.desc())
            .first()
        )
        return _to_result(record) if record else None
    finally:
        session.close()


def list_approvals() -> list[ApprovalResult]:
    """Return all approvals, newest first."""
    session = get_session_factory()()
    try:
        records = (
            session.query(ApprovalRequestRecord)
            .order_by(ApprovalRequestRecord.id.desc())
            .all()
        )
        return [_to_result(r) for r in records]
    finally:
        session.close()


def update_status(
    approval_id: int,
    status: ApprovalStatus,
    decided_by: Optional[str] = None,
    decision_comment: str = "",
) -> Optional[ApprovalResult]:
    """Update an approval's status (and decision metadata). No deletes."""
    session = get_session_factory()()
    try:
        record = session.get(ApprovalRequestRecord, approval_id)
        if record is None:
            return None
        record.status = ApprovalStatus(status).value
        if decided_by is not None:
            record.decided_by = decided_by
        if decision_comment:
            record.decision_comment = decision_comment
        if ApprovalStatus(status) in (
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.INVALIDATED,
        ):
            record.decided_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(record)
        return _to_result(record)
    finally:
        session.close()


def get_summary() -> ApprovalSummary:
    """Aggregate approval counts by status."""
    approvals = list_approvals()
    summary = ApprovalSummary(total=len(approvals))
    for approval in approvals:
        status = ApprovalStatus(approval.status)
        if status == ApprovalStatus.PENDING:
            summary.pending += 1
        elif status == ApprovalStatus.APPROVED:
            summary.approved += 1
        elif status == ApprovalStatus.REJECTED:
            summary.rejected += 1
        elif status == ApprovalStatus.INVALIDATED:
            summary.invalidated += 1
    return summary
