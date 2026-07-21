"""Deployment persistence — Phase 7.

Saves and loads deployment runs to the deployment_runs table. Stores no
secrets and no real Fabric identifiers (mock mode only).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from src.database import DeploymentRunRecord, get_session_factory
from src.models.schemas import DeploymentResult

logger = logging.getLogger(__name__)

_FORBIDDEN_TOKENS = (
    "client_secret",
    "accountKey",
    "connectionString",
    "accessToken",
    "servicePrincipalKey",
)


def _assert_no_credentials(payload: str) -> None:
    found = [tok for tok in _FORBIDDEN_TOKENS if tok in payload]
    if found:
        raise ValueError(
            f"Refusing to persist deployment: credential tokens found: {found}"
        )


def save_deployment(result: DeploymentResult) -> dict:
    """Persist a DeploymentResult and return its record.

    Returns {id, plan_id, approval_id, mode, status, created_at,
    completed_at, result}.
    """
    payload = json.dumps(result.model_dump(mode="json"))
    _assert_no_credentials(payload)

    session = get_session_factory()()
    try:
        record = DeploymentRunRecord(
            plan_id=result.plan_id,
            approval_id=result.approval_id,
            mode=result.mode.value,
            status=result.status.value,
            result_json=payload,
            completed_at=datetime.now(timezone.utc),
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        logger.info(
            "Saved deployment id=%d plan=%d mode=%s status=%s.",
            record.id, result.plan_id, result.mode.value, result.status.value,
        )
        return _to_record(record)
    finally:
        session.close()


def _to_record(record: DeploymentRunRecord) -> dict:
    return {
        "id": record.id,
        "plan_id": record.plan_id,
        "approval_id": record.approval_id,
        "mode": record.mode,
        "status": record.status,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "completed_at": (
            record.completed_at.isoformat() if record.completed_at else None
        ),
        "result": DeploymentResult(**json.loads(record.result_json)),
    }


def get_deployment(deployment_id: int) -> Optional[dict]:
    """Load a single deployment by id, or None."""
    session = get_session_factory()()
    try:
        record = session.get(DeploymentRunRecord, deployment_id)
        return _to_record(record) if record else None
    finally:
        session.close()


def get_latest_deployment() -> Optional[dict]:
    """Load the most recent deployment, or None."""
    session = get_session_factory()()
    try:
        record = (
            session.query(DeploymentRunRecord)
            .order_by(DeploymentRunRecord.id.desc())
            .first()
        )
        return _to_record(record) if record else None
    finally:
        session.close()


def list_deployments() -> list[dict]:
    """Return metadata for all deployments, newest first (no bodies)."""
    session = get_session_factory()()
    try:
        records = (
            session.query(DeploymentRunRecord)
            .order_by(DeploymentRunRecord.id.desc())
            .all()
        )
        return [
            {
                "id": r.id,
                "plan_id": r.plan_id,
                "approval_id": r.approval_id,
                "mode": r.mode,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "completed_at": (
                    r.completed_at.isoformat() if r.completed_at else None
                ),
            }
            for r in records
        ]
    finally:
        session.close()
