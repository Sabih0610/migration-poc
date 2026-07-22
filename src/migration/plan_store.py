"""Migration plan persistence — Phase 5.

Saves and loads MigrationPlan objects to the migration_plans table.
Plans are versioned per assessment (v1, v2, ...). Never persists
credentials: the plan models carry none and a defensive scan rejects
any payload that looks like it contains one.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

from src.artifacts import (
    ArtifactPackageError,
    canonical_json,
    verify_saved_package,
    write_package,
)
from src.config import get_settings
from src.database import MigrationPlanRecord, get_session_factory
from src.models.schemas import MigrationPlan

logger = logging.getLogger(__name__)


def compute_plan_package_fingerprint(plan: MigrationPlan) -> str:
    """Return a deterministic SHA-256 fingerprint of a plan's content.

    Uses a canonical (sorted-key) JSON encoding so the same plan always
    yields the same fingerprint and any content change alters it.
    """
    payload = canonical_json(_fingerprint_payload(plan.model_dump(mode="json")))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Backward-compatible name; there is intentionally only one canonical
# fingerprint implementation for plan and generated-package content.
compute_plan_fingerprint = compute_plan_package_fingerprint


_FINGERPRINT_EXCLUDED_KEYS = {
    "plan_id",
    "plan_version",
    "assessment_id",
    "approval_id",
    "deployment_id",
    "discovery_id",
    "validation_id",
    "database_id",
    "output_directory",
    "absolute_path",
    "package_path",
    "created_at",
    "updated_at",
    "generated_at",
    "started_at",
    "completed_at",
}


def _fingerprint_payload(value):
    """Strip persistence/time/location metadata from canonical content."""
    if isinstance(value, dict):
        return {
            key: _fingerprint_payload(item)
            for key, item in value.items()
            if key not in _FINGERPRINT_EXCLUDED_KEYS
        }
    if isinstance(value, list):
        return [_fingerprint_payload(item) for item in value]
    return value

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
        package_manifest_path = _write_generated_package(plan)
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
        result = _to_record(record)
        result["package_manifest_path"] = package_manifest_path
        return result
    finally:
        session.close()


def _to_record(record: MigrationPlanRecord) -> dict:
    """Convert an ORM row into a plain record with a parsed plan."""
    plan = MigrationPlan(**json.loads(record.plan_json))
    return {
        "id": record.id,
        "assessment_id": record.assessment_id,
        "version": record.version,
        "executable": record.executable,
        "overall_risk": record.overall_risk,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "plan": plan,
        "package_manifest_path": _package_manifest_path(plan),
    }


def _write_generated_package(plan: MigrationPlan) -> Optional[str]:
    package = plan.generated_package
    if package is None:
        return None
    root = Path(get_settings().generated_artifacts_dir).resolve()
    manifest_path = write_package(package, root)
    return manifest_path.relative_to(root).as_posix()


def _package_manifest_path(plan: MigrationPlan) -> Optional[str]:
    if plan.generated_package is None:
        return None
    return f"manifests/{plan.generated_package.package_id}.json"


def verify_plan_package(plan: MigrationPlan):
    """Verify the persisted manifest/files exactly match the plan package."""
    if plan.generated_package is None:
        raise ArtifactPackageError("plan has no generated artifact package")
    root = Path(get_settings().generated_artifacts_dir).resolve()
    return verify_saved_package(plan.generated_package, root)


def _package_id_from_json(payload: str) -> Optional[str]:
    package = json.loads(payload).get("generated_package")
    return package.get("package_id") if package else None


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
                "package_id": _package_id_from_json(r.plan_json),
            }
            for r in records
        ]
    finally:
        session.close()
