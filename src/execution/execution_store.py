"""Execution persistence — Phase 11.

Saves and loads controlled source/target pipeline executions to the
``pipeline_executions`` table. Stores only safe run metadata — the ORM
record (``PipelineExecutionRecord``) has no free-form "data"/"payload"
column, so customer row content can never be persisted here, even by
mistake. Survives process restart: it is the same SQLite database file
used by every other store in this codebase (see ``src/database.py``).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from src.database import PipelineExecutionRecord, get_session_factory
from src.models.schemas import ExecutionSide, ExecutionStatus, PipelineExecutionResult, RuntimeMetrics

logger = logging.getLogger(__name__)

# Statuses that mean "still in flight" for duplicate-prevention purposes.
_ACTIVE_STATUSES = {ExecutionStatus.QUEUED.value, ExecutionStatus.RUNNING.value}


class DuplicateExecutionError(Exception):
    """Raised when an execution is already running for this pipeline."""

    def __init__(self, message: str, existing_execution_id: int):
        super().__init__(message)
        self.message = message
        self.existing_execution_id = existing_execution_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def start_execution(
    *,
    correlation_id: str,
    side: ExecutionSide,
    pipeline_identity: str,
    plan_id: Optional[int] = None,
    deployment_id: Optional[int] = None,
    discovery_snapshot_id: Optional[int] = None,
) -> PipelineExecutionResult:
    """Create a RUNNING execution record, rejecting concurrent duplicates.

    A second execution for the same side + pipeline identity while one is
    already QUEUED/RUNNING is rejected rather than silently duplicated.
    """
    session = get_session_factory()()
    try:
        existing = (
            session.query(PipelineExecutionRecord)
            .filter(
                PipelineExecutionRecord.side == ExecutionSide(side).value,
                PipelineExecutionRecord.pipeline_identity == pipeline_identity,
                PipelineExecutionRecord.status.in_(_ACTIVE_STATUSES),
            )
            .order_by(PipelineExecutionRecord.id.desc())
            .first()
        )
        if existing is not None:
            raise DuplicateExecutionError(
                f"An execution for {side} pipeline '{pipeline_identity}' is "
                f"already {existing.status} (execution_id={existing.id}).",
                existing.id,
            )
        record = PipelineExecutionRecord(
            correlation_id=correlation_id,
            side=ExecutionSide(side).value,
            pipeline_identity=pipeline_identity,
            plan_id=plan_id,
            deployment_id=deployment_id,
            discovery_snapshot_id=discovery_snapshot_id,
            status=ExecutionStatus.RUNNING.value,
            started_at=_now(),
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        logger.info(
            "Started execution id=%d side=%s pipeline=%r.",
            record.id, record.side, pipeline_identity,
        )
        return _to_result(record)
    finally:
        session.close()


def complete_execution(
    execution_id: int,
    *,
    status: ExecutionStatus,
    run_id: Optional[str] = None,
    safe_error_category: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    metrics: Optional[RuntimeMetrics] = None,
) -> PipelineExecutionResult:
    """Finalize an execution record with a terminal status and safe metrics."""
    session = get_session_factory()()
    try:
        record = session.get(PipelineExecutionRecord, execution_id)
        if record is None:
            raise ValueError(f"Execution {execution_id} not found.")
        record.status = ExecutionStatus(status).value
        record.run_id = run_id
        record.safe_error_category = safe_error_category
        record.duration_seconds = int(duration_seconds) if duration_seconds is not None else None
        record.completed_at = _now()
        if metrics is not None:
            record.metrics_json = json.dumps(metrics.model_dump(mode="json"))
        session.add(record)
        session.commit()
        session.refresh(record)
        logger.info(
            "Completed execution id=%d status=%s.", record.id, record.status
        )
        return _to_result(record)
    finally:
        session.close()


def _to_result(record: PipelineExecutionRecord) -> PipelineExecutionResult:
    metrics = None
    if record.metrics_json:
        try:
            metrics = RuntimeMetrics(**json.loads(record.metrics_json))
        except (ValueError, TypeError):
            metrics = None
    return PipelineExecutionResult(
        execution_id=record.id,
        correlation_id=record.correlation_id,
        side=ExecutionSide(record.side),
        pipeline_identity=record.pipeline_identity,
        run_id=record.run_id,
        plan_id=record.plan_id,
        deployment_id=record.deployment_id,
        discovery_snapshot_id=record.discovery_snapshot_id,
        status=ExecutionStatus(record.status),
        safe_error_category=record.safe_error_category,
        started_at=record.started_at.isoformat() if record.started_at else None,
        completed_at=record.completed_at.isoformat() if record.completed_at else None,
        duration_seconds=record.duration_seconds,
        metrics=metrics,
    )


def get_execution(execution_id: int) -> Optional[PipelineExecutionResult]:
    session = get_session_factory()()
    try:
        record = session.get(PipelineExecutionRecord, execution_id)
        return _to_result(record) if record else None
    finally:
        session.close()


def list_executions(
    *,
    side: Optional[ExecutionSide] = None,
    plan_id: Optional[int] = None,
    correlation_id: Optional[str] = None,
    status: Optional[ExecutionStatus] = None,
) -> list[PipelineExecutionResult]:
    """Return executions newest-first, optionally filtered."""
    session = get_session_factory()()
    try:
        query = session.query(PipelineExecutionRecord)
        if side is not None:
            query = query.filter(PipelineExecutionRecord.side == ExecutionSide(side).value)
        if plan_id is not None:
            query = query.filter(PipelineExecutionRecord.plan_id == plan_id)
        if correlation_id is not None:
            query = query.filter(PipelineExecutionRecord.correlation_id == correlation_id)
        if status is not None:
            query = query.filter(PipelineExecutionRecord.status == ExecutionStatus(status).value)
        records = query.order_by(PipelineExecutionRecord.id.desc()).all()
        return [_to_result(record) for record in records]
    finally:
        session.close()


def get_running_execution(
    side: ExecutionSide, pipeline_identity: str
) -> Optional[PipelineExecutionResult]:
    session = get_session_factory()()
    try:
        record = (
            session.query(PipelineExecutionRecord)
            .filter(
                PipelineExecutionRecord.side == ExecutionSide(side).value,
                PipelineExecutionRecord.pipeline_identity == pipeline_identity,
                PipelineExecutionRecord.status.in_(_ACTIVE_STATUSES),
            )
            .order_by(PipelineExecutionRecord.id.desc())
            .first()
        )
        return _to_result(record) if record else None
    finally:
        session.close()
