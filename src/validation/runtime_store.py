"""Persistence for optional runtime/metric validation runs."""

from datetime import datetime, timezone
from typing import Optional

from src.database import RuntimeValidationRunRecord, get_session_factory
from src.models.schemas import ValidationResult


def _to_result(record: RuntimeValidationRunRecord) -> ValidationResult:
    return ValidationResult.model_validate_json(record.result_json)


def save_runtime_validation(result: ValidationResult) -> ValidationResult:
    session = get_session_factory()()
    try:
        record = RuntimeValidationRunRecord(
            deployment_id=result.deployment_id,
            plan_id=result.plan_id,
            status=result.status.value,
            result_json="{}",
            completed_at=datetime.now(timezone.utc),
        )
        session.add(record)
        session.flush()
        result.validation_id = record.id
        record.result_json = result.model_dump_json()
        session.commit()
        return result
    finally:
        session.close()


def get_latest_runtime_validation() -> Optional[ValidationResult]:
    session = get_session_factory()()
    try:
        record = (
            session.query(RuntimeValidationRunRecord)
            .order_by(RuntimeValidationRunRecord.id.desc())
            .first()
        )
        return _to_result(record) if record else None
    finally:
        session.close()
