"""Persistence for artifact structural validation runs."""

from datetime import datetime, timezone
from typing import Optional

from src.database import StructuralValidationRunRecord, get_session_factory
from src.models.schemas import StructuralValidationResult


def _to_result(record: StructuralValidationRunRecord) -> StructuralValidationResult:
    return StructuralValidationResult.model_validate_json(record.result_json)


def save_structural_validation(
    result: StructuralValidationResult,
) -> StructuralValidationResult:
    session = get_session_factory()()
    try:
        record = StructuralValidationRunRecord(
            discovery_id=result.discovery_id,
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


def get_structural_validation(
    validation_id: int,
) -> Optional[StructuralValidationResult]:
    session = get_session_factory()()
    try:
        record = session.get(StructuralValidationRunRecord, validation_id)
        return _to_result(record) if record else None
    finally:
        session.close()


def get_latest_structural_validation() -> Optional[StructuralValidationResult]:
    session = get_session_factory()()
    try:
        record = (
            session.query(StructuralValidationRunRecord)
            .order_by(StructuralValidationRunRecord.id.desc())
            .first()
        )
        return _to_result(record) if record else None
    finally:
        session.close()
