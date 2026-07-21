"""Migration plan persistence — Phase 5.

Saves and loads MigrationPlan objects to the migration_plans table.
Plans are versioned per assessment (v1, v2, ...). Never persists
credentials: the plan models carry none and a defensive scan rejects
any payload that looks like it contains one.
"""

import hashlib
import json
import logging
from typing import Optional

from src.database import MigrationPlanRecord, get_session_factory
from src.models.schemas import MigrationPlan

logger = logging.getLogger(__name__)


def compute_plan_fingerprint(plan: MigrationPlan) -> str:
    """Return a deterministic SHA-256 fingerprint of a plan's content.

    Uses a canonical (sorted-key) JSON encoding so the same plan always
    yields the same fingerprint and any content change alters it.
    """
    payload = json.dumps(
        plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

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
            f"Refusing to persist plan: credential-like tokens found: {found}"
        )


def _next_version(session, assessment_id: Optional[int]) -> int:
    """Return the next version number for a given assessment."""
    latest = (
        session.query(MigrationPlanRecord)
        .filter(MigrationPlanRecord.assessment_id == assessment_id)
        .order_by(MigrationPlanRecord.version.desc())
        .first()
    )
    return (latest.version + 1) if latest else 1


def save_plan(plan: MigrationPlan, assessment_id: Optional[int] = None) -> dict:
    """Persist a MigrationPlan (auto-versioned) and return its record.

    Returns a dict: {id, assessment_id, version, executable,
    overall_risk, created_at, plan}.
    """
    payload = json.dumps(plan.model_dump(mode="json"))
    _assert_no_credentials(payload)

    session = get_session_factory()()
    try:
        version = _next_version(session, assessment_id)
        record = MigrationPlanRecord(
            assessment_id=assessment_id,
            version=version,
            executable=plan.executable,
            overall_risk=plan.overall_risk.value,
            plan_json=payload,
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        logger.info(
            "Saved plan id=%d (assessment=%s, v%d).",
            record.id, assessment_id, version,
        )
        return _to_record(record)
    finally:
        session.close()


def _to_record(record: MigrationPlanRecord) -> dict:
    """Convert an ORM row into a plain record with a parsed plan."""
    return {
        "id": record.id,
        "assessment_id": record.assessment_id,
        "version": record.version,
        "executable": record.executable,
        "overall_risk": record.overall_risk,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "plan": MigrationPlan(**json.loads(record.plan_json)),
    }


def get_plan(plan_id: int) -> Optional[dict]:
    """Load a single plan by id, or None if not found."""
    session = get_session_factory()()
    try:
        record = session.get(MigrationPlanRecord, plan_id)
        return _to_record(record) if record else None
    finally:
        session.close()


def get_latest_plan() -> Optional[dict]:
    """Load the most recently created plan, or None if none exist."""
    session = get_session_factory()()
    try:
        record = (
            session.query(MigrationPlanRecord)
            .order_by(MigrationPlanRecord.id.desc())
            .first()
        )
        return _to_record(record) if record else None
    finally:
        session.close()


def list_plans() -> list[dict]:
    """Return metadata for all plans, newest first (no plan bodies)."""
    session = get_session_factory()()
    try:
        records = (
            session.query(MigrationPlanRecord)
            .order_by(MigrationPlanRecord.id.desc())
            .all()
        )
        return [
            {
                "id": r.id,
                "assessment_id": r.assessment_id,
                "version": r.version,
                "executable": r.executable,
                "overall_risk": r.overall_risk,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in records
        ]
    finally:
        session.close()
