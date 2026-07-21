"""Assessment persistence — Phase 4.

Saves and loads AssessmentResult objects to the assessment_runs table.
Never persists credentials: the result models carry none, and a
defensive scan rejects any payload that looks like it contains one.
"""

import json
import logging
from typing import Optional

from src.database import AssessmentRun, get_session_factory
from src.models.schemas import AssessmentResult

logger = logging.getLogger(__name__)

# High-signal credential tokens that must never reach the database.
_FORBIDDEN_TOKENS = (
    "client_secret",
    "accountKey",
    "connectionString",
    "accessToken",
    "servicePrincipalKey",
)


def _assert_no_credentials(payload: str) -> None:
    """Raise if the serialized payload contains a credential token."""
    found = [tok for tok in _FORBIDDEN_TOKENS if tok in payload]
    if found:
        raise ValueError(
            f"Refusing to persist assessment: credential-like tokens found: {found}"
        )


def save_assessment(result: AssessmentResult) -> int:
    """Persist an AssessmentResult and return its new row id."""
    payload = json.dumps(result.model_dump(mode="json"))
    _assert_no_credentials(payload)

    session = get_session_factory()()
    try:
        run = AssessmentRun(
            overall_status=result.overall_status.value,
            result_json=payload,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        logger.info("Saved assessment run id=%d (%s).", run.id, run.overall_status)
        return run.id
    finally:
        session.close()


def _to_record(run: AssessmentRun) -> dict:
    """Convert an ORM row into a plain record with a parsed result."""
    return {
        "id": run.id,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "overall_status": run.overall_status,
        "result": AssessmentResult(**json.loads(run.result_json)),
    }


def get_assessment(assessment_id: int) -> Optional[dict]:
    """Load a single assessment run by id, or None if not found.

    Returns a dict: {id, created_at, overall_status, result}.
    """
    session = get_session_factory()()
    try:
        run = session.get(AssessmentRun, assessment_id)
        if run is None:
            return None
        return _to_record(run)
    finally:
        session.close()


def get_latest_assessment() -> Optional[dict]:
    """Load the most recent assessment run, or None if none exist."""
    session = get_session_factory()()
    try:
        run = (
            session.query(AssessmentRun)
            .order_by(AssessmentRun.id.desc())
            .first()
        )
        if run is None:
            return None
        return _to_record(run)
    finally:
        session.close()
