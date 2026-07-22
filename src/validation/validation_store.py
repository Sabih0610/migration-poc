"""Validation persistence store."""

import json
import logging
from typing import Optional
from datetime import datetime

from src.database import ValidationRunRecord, get_session_factory
from src.models.schemas import ValidationResult

logger = logging.getLogger(__name__)

def save_validation(result: ValidationResult) -> int:
    """Save a validation run to the database."""
    session_factory = get_session_factory()
    with session_factory() as db:
        try:
            record = ValidationRunRecord(
                deployment_id=result.deployment_id,
                plan_id=result.plan_id,
                status=result.status.value,
                result_json=result.model_dump_json(),
                created_at=datetime.fromisoformat(result.started_at) if result.started_at else datetime.now(),
                completed_at=datetime.fromisoformat(result.completed_at) if result.completed_at else None
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            return record.id
        except Exception as e:
            logger.error(f"Failed to save validation: {e}")
            db.rollback()
            raise

def get_validation(validation_id: int) -> Optional[ValidationResult]:
    """Retrieve a specific validation run by ID."""
    session_factory = get_session_factory()
    with session_factory() as db:
        record = db.query(ValidationRunRecord).filter(ValidationRunRecord.id == validation_id).first()
        if not record:
            return None
        return ValidationResult.model_validate_json(record.result_json)

def get_latest_validation() -> Optional[ValidationResult]:
    """Retrieve the most recent validation run."""
    session_factory = get_session_factory()
    with session_factory() as db:
        record = db.query(ValidationRunRecord).order_by(ValidationRunRecord.id.desc()).first()
        if not record:
            return None
        return ValidationResult.model_validate_json(record.result_json)

def list_validations(limit: int = 10) -> list[ValidationResult]:
    """Retrieve recent validation runs."""
    session_factory = get_session_factory()
    with session_factory() as db:
        records = db.query(ValidationRunRecord).order_by(ValidationRunRecord.id.desc()).limit(limit).all()
        return [ValidationResult.model_validate_json(r.result_json) for r in records]
