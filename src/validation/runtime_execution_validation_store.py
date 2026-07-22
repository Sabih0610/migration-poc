"""Persistence for execution-linked runtime-equivalence validation runs
(Phase 11). Distinct from ``src.validation.runtime_store`` (Phase 8's
metrics-only comparison, not tied to a real execution)."""

from typing import Optional

from src.database import RuntimeExecutionValidationRecord, get_session_factory
from src.models.schemas import RuntimeValidationResult


def _to_result(record: RuntimeExecutionValidationRecord) -> RuntimeValidationResult:
    return RuntimeValidationResult.model_validate_json(record.result_json)


def save_runtime_execution_validation(
    result: RuntimeValidationResult,
) -> RuntimeValidationResult:
    session = get_session_factory()()
    try:
        record = RuntimeExecutionValidationRecord(
            plan_id=result.plan_id,
            deployment_id=result.deployment_id,
            correlation_id=result.correlation_id,
            status=result.status.value,
            result_json="{}",
        )
        session.add(record)
        session.flush()
        result.validation_id = record.id
        record.result_json = result.model_dump_json()
        session.commit()
        session.refresh(record)
        return _to_result(record)
    finally:
        session.close()


def get_runtime_execution_validation(
    validation_id: int,
) -> Optional[RuntimeValidationResult]:
    session = get_session_factory()()
    try:
        record = session.get(RuntimeExecutionValidationRecord, validation_id)
        return _to_result(record) if record else None
    finally:
        session.close()


def get_latest_runtime_execution_validation(
    plan_id: Optional[int] = None,
) -> Optional[RuntimeValidationResult]:
    session = get_session_factory()()
    try:
        query = session.query(RuntimeExecutionValidationRecord)
        if plan_id is not None:
            query = query.filter(RuntimeExecutionValidationRecord.plan_id == plan_id)
        record = query.order_by(RuntimeExecutionValidationRecord.id.desc()).first()
        return _to_result(record) if record else None
    finally:
        session.close()
