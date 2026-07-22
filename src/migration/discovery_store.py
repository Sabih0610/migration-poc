"""Persistence for complete source-definition discovery snapshots."""

import json
import logging
from typing import Optional

from src.database import DiscoveryRunRecord, get_session_factory
from src.models.schemas import DiscoveryResult

logger = logging.getLogger(__name__)

_REDACTED = "***REDACTED***"
# Keys whose *unredacted string* values would be secrets. Redacted values
# and non-string values (e.g. Key Vault reference objects) are allowed.
_FORBIDDEN_KEYS = {
    "client_secret",
    "clientsecret",
    "accountkey",
    "connectionstring",
    "accesstoken",
    "serviceprincipalkey",
    "password",
    "secret",
    "sastoken",
}


def _find_unredacted_secret(node) -> str | None:
    if isinstance(node, dict):
        for key, value in node.items():
            if (
                str(key).lower() in _FORBIDDEN_KEYS
                and isinstance(value, str)
                and value
                and value != _REDACTED
            ):
                return str(key)
            hit = _find_unredacted_secret(value)
            if hit:
                return hit
    elif isinstance(node, list):
        for item in node:
            hit = _find_unredacted_secret(item)
            if hit:
                return hit
    return None


def _assert_no_credentials(data) -> None:
    hit = _find_unredacted_secret(data)
    if hit:
        raise ValueError(
            "Refusing to persist discovery snapshot: unredacted credential "
            f"value found under key '{hit}'."
        )


def save_discovery(result: DiscoveryResult) -> dict:
    """Persist and return a complete discovery snapshot record."""
    data = result.model_dump(mode="json", by_alias=True)
    _assert_no_credentials(data)
    payload = json.dumps(data)

    session = get_session_factory()()
    try:
        record = DiscoveryRunRecord(
            artifact_count=result.summary.artifact_count,
            component_count=result.summary.component_count,
            result_json=payload,
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        logger.info(
            "Saved discovery snapshot id=%d (%d artifacts, %d components).",
            record.id,
            record.artifact_count,
            record.component_count,
        )
        return _to_record(record)
    finally:
        session.close()


def _to_record(record: DiscoveryRunRecord) -> dict:
    return {
        "id": record.id,
        "artifact_count": record.artifact_count,
        "component_count": record.component_count,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "result": DiscoveryResult(**json.loads(record.result_json)),
    }


def get_discovery(discovery_id: int) -> Optional[dict]:
    session = get_session_factory()()
    try:
        record = session.get(DiscoveryRunRecord, discovery_id)
        return _to_record(record) if record else None
    finally:
        session.close()


def get_latest_discovery() -> Optional[dict]:
    session = get_session_factory()()
    try:
        record = (
            session.query(DiscoveryRunRecord)
            .order_by(DiscoveryRunRecord.id.desc())
            .first()
        )
        return _to_record(record) if record else None
    finally:
        session.close()


def list_discoveries() -> list[dict]:
    """Return snapshot metadata, newest first, without large definitions."""
    session = get_session_factory()()
    try:
        records = (
            session.query(DiscoveryRunRecord)
            .order_by(DiscoveryRunRecord.id.desc())
            .all()
        )
        return [
            {
                "id": record.id,
                "artifact_count": record.artifact_count,
                "component_count": record.component_count,
                "created_at": (
                    record.created_at.isoformat() if record.created_at else None
                ),
            }
            for record in records
        ]
    finally:
        session.close()
