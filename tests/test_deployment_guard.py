"""Tests for the Phase 6 deployment guard (mostly negative)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database as db_module
from src.approvals.approval_store import save_approval, update_status
from src.approvals.deployment_guard import (
    DeploymentAuthorizationError,
    validate_deployment_authorization,
)
from src.database import Base
from src.migration.plan_store import compute_plan_fingerprint, save_plan
from src.models.schemas import (
    ApprovalStatus,
    MigrationAction,
    MigrationActionType,
    MigrationPlan,
    MigrationRisk,
    TargetItemType,
)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(db_module, "_SessionLocal", session_factory)
    yield engine


def _mk_plan(executable=True, delete=False) -> MigrationPlan:
    actions = [
        MigrationAction(
            order=1,
            action_type=MigrationActionType.VERIFY_WORKSPACE,
            target_item_type=TargetItemType.WORKSPACE,
            target_item_name="ws",
            reason="verify",
        )
    ]
    if delete:
        actions.append(
            MigrationAction(
                order=2,
                action_type=MigrationActionType.CREATE_TABLE,
                target_item_type=TargetItemType.LAKEHOUSE_TABLE,
                target_item_name="t",
                reason="delete the old staging table before load",
            )
        )
    return MigrationPlan(
        executable=executable, overall_risk=MigrationRisk.MEDIUM, actions=actions
    )


def _seed_approved(plan_rec, version=None, fingerprint=None):
    """Save an APPROVED approval for a plan record."""
    fp = fingerprint or compute_plan_fingerprint(plan_rec["plan"])
    ver = version if version is not None else plan_rec["version"]
    appr = save_approval(plan_rec["id"], ver, fp, "alice")
    update_status(appr.approval_id, ApprovalStatus.APPROVED, decided_by="bob")
    return appr.approval_id


# ── Positive ─────────────────────────────────────────────────────


def test_authorized_success(temp_db):
    rec = save_plan(_mk_plan(), assessment_id=1)
    approval_id = _seed_approved(rec)
    result = validate_deployment_authorization(rec["id"], approval_id)
    assert result.authorized is True
    assert "no_delete_action" in result.checks_passed


# ── Negative ─────────────────────────────────────────────────────


def test_unknown_plan(temp_db):
    with pytest.raises(DeploymentAuthorizationError) as exc:
        validate_deployment_authorization(999, 1)
    assert exc.value.code == "PLAN_NOT_FOUND"


def test_unknown_approval(temp_db):
    rec = save_plan(_mk_plan(), assessment_id=1)
    with pytest.raises(DeploymentAuthorizationError) as exc:
        validate_deployment_authorization(rec["id"], 999)
    assert exc.value.code == "APPROVAL_NOT_FOUND"


def test_plan_id_mismatch(temp_db):
    rec_a = save_plan(_mk_plan(), assessment_id=1)
    rec_b = save_plan(_mk_plan(), assessment_id=2)
    approval_id = _seed_approved(rec_a)
    with pytest.raises(DeploymentAuthorizationError) as exc:
        validate_deployment_authorization(rec_b["id"], approval_id)
    assert exc.value.code == "PLAN_ID_MISMATCH"


def test_pending_not_approved(temp_db):
    rec = save_plan(_mk_plan(), assessment_id=1)
    fp = compute_plan_fingerprint(rec["plan"])
    appr = save_approval(rec["id"], rec["version"], fp, "alice")  # stays PENDING
    with pytest.raises(DeploymentAuthorizationError) as exc:
        validate_deployment_authorization(rec["id"], appr.approval_id)
    assert exc.value.code == "NOT_APPROVED"


def test_invalidated_blocked(temp_db):
    rec = save_plan(_mk_plan(), assessment_id=1)
    fp = compute_plan_fingerprint(rec["plan"])
    appr = save_approval(rec["id"], rec["version"], fp, "alice")
    update_status(appr.approval_id, ApprovalStatus.INVALIDATED, decided_by="system")
    with pytest.raises(DeploymentAuthorizationError) as exc:
        validate_deployment_authorization(rec["id"], appr.approval_id)
    assert exc.value.code == "APPROVAL_INVALIDATED"


def test_version_mismatch(temp_db):
    rec = save_plan(_mk_plan(), assessment_id=1)
    approval_id = _seed_approved(rec, version=rec["version"] + 1)
    with pytest.raises(DeploymentAuthorizationError) as exc:
        validate_deployment_authorization(rec["id"], approval_id)
    assert exc.value.code == "VERSION_MISMATCH"


def test_fingerprint_mismatch(temp_db):
    rec = save_plan(_mk_plan(), assessment_id=1)
    approval_id = _seed_approved(rec, fingerprint="deadbeef")
    with pytest.raises(DeploymentAuthorizationError) as exc:
        validate_deployment_authorization(rec["id"], approval_id)
    assert exc.value.code == "FINGERPRINT_MISMATCH"


def test_non_executable_plan_blocked(temp_db):
    rec = save_plan(_mk_plan(executable=False), assessment_id=1)
    approval_id = _seed_approved(rec)
    with pytest.raises(DeploymentAuthorizationError) as exc:
        validate_deployment_authorization(rec["id"], approval_id)
    assert exc.value.code == "PLAN_NOT_EXECUTABLE"


def test_delete_action_blocked(temp_db):
    rec = save_plan(_mk_plan(delete=True), assessment_id=1)
    approval_id = _seed_approved(rec)
    with pytest.raises(DeploymentAuthorizationError) as exc:
        validate_deployment_authorization(rec["id"], approval_id)
    assert exc.value.code == "DELETE_ACTION_PRESENT"
