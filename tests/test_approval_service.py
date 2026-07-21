"""Tests for the Phase 6 approval service."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database as db_module
from src.approvals import approval_service as svc
from src.approvals.approval_store import save_approval, update_status
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


def _mk_plan(executable=True) -> MigrationPlan:
    return MigrationPlan(
        executable=executable,
        overall_risk=MigrationRisk.MEDIUM,
        actions=[
            MigrationAction(
                order=1,
                action_type=MigrationActionType.VERIFY_WORKSPACE,
                target_item_type=TargetItemType.WORKSPACE,
                target_item_name="ws",
                reason="verify",
            )
        ],
    )


def _save(executable=True, assessment_id=1):
    return save_plan(_mk_plan(executable), assessment_id=assessment_id)


# ── Requesting ───────────────────────────────────────────────────


def test_request_approval_works(temp_db):
    rec = _save()
    result = svc.request_approval(rec["id"], "alice", "review please")
    assert result.status == ApprovalStatus.PENDING
    assert result.plan_id == rec["id"]
    assert result.requested_by == "alice"


def test_request_requires_existing_plan(temp_db):
    with pytest.raises(svc.ApprovalError) as exc:
        svc.request_approval(999, "alice")
    assert exc.value.code == "PLAN_NOT_FOUND"


def test_request_requires_executable_plan(temp_db):
    rec = _save(executable=False)
    with pytest.raises(svc.ApprovalError) as exc:
        svc.request_approval(rec["id"], "alice")
    assert exc.value.code == "NOT_EXECUTABLE"


# ── Deciding ─────────────────────────────────────────────────────


def test_approve_then_can_deploy(temp_db):
    rec = _save()
    appr = svc.request_approval(rec["id"], "alice")
    assert svc.can_deploy(rec["id"], appr.approval_id) is False  # pending
    svc.approve(appr.approval_id, "bob", "ok")
    assert svc.can_deploy(rec["id"], appr.approval_id) is True


def test_reject_cannot_deploy(temp_db):
    rec = _save()
    appr = svc.request_approval(rec["id"], "alice")
    svc.reject(appr.approval_id, "bob", "no")
    assert svc.can_deploy(rec["id"], appr.approval_id) is False


def test_duplicate_decision_blocked(temp_db):
    rec = _save()
    appr = svc.request_approval(rec["id"], "alice")
    svc.approve(appr.approval_id, "bob")
    with pytest.raises(svc.ApprovalError) as exc:
        svc.approve(appr.approval_id, "bob")
    assert exc.value.code == "INVALID_TRANSITION"


def test_decide_unknown_approval(temp_db):
    with pytest.raises(svc.ApprovalError) as exc:
        svc.approve(999, "bob")
    assert exc.value.code == "APPROVAL_NOT_FOUND"


# ── Invalidation ─────────────────────────────────────────────────


def test_new_version_invalidates_old_approval(temp_db):
    v1 = _save(assessment_id=42)
    appr = svc.request_approval(v1["id"], "alice")
    svc.approve(appr.approval_id, "bob")
    assert svc.can_deploy(v1["id"], appr.approval_id) is True

    # A new version for the same assessment supersedes it.
    v2 = _save(assessment_id=42)
    invalidated = svc.invalidate_stale_approvals(v2["id"])
    assert appr.approval_id in invalidated
    assert svc.get_status(appr.approval_id).status == ApprovalStatus.INVALIDATED
    assert svc.can_deploy(v1["id"], appr.approval_id) is False


def test_changed_fingerprint_invalidates_on_decision(temp_db):
    rec = _save()
    # Save an approval whose fingerprint does not match the plan.
    appr = save_approval(rec["id"], rec["version"], "wrongfp", "alice")
    with pytest.raises(svc.ApprovalError) as exc:
        svc.approve(appr.approval_id, "bob")
    assert exc.value.code == "INVALIDATED"
    assert svc.get_status(appr.approval_id).status == ApprovalStatus.INVALIDATED


def test_invalidated_cannot_deploy(temp_db):
    rec = _save()
    fp = compute_plan_fingerprint(rec["plan"])
    appr = save_approval(rec["id"], rec["version"], fp, "alice")
    update_status(appr.approval_id, ApprovalStatus.INVALIDATED, decided_by="system")
    assert svc.can_deploy(rec["id"], appr.approval_id) is False
