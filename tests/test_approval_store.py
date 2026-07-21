"""Tests for Phase 6 approval persistence (SQLite)."""

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import src.database as db_module
from src.database import Base
from src.approvals.approval_store import (
    get_approval,
    get_latest_for_plan,
    get_summary,
    list_approvals,
    save_approval,
    update_status,
)
from src.models.schemas import ApprovalStatus


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


def test_table_created(temp_db):
    assert "approval_requests" in inspect(temp_db).get_table_names()


def test_save_creates_pending(temp_db):
    result = save_approval(1, 1, "fp123", "alice", "please review")
    assert result.approval_id > 0
    assert result.status == ApprovalStatus.PENDING
    assert result.requested_by == "alice"
    assert result.request_comment == "please review"
    assert result.request_time is not None
    assert result.decision_time is None


def test_get_and_latest(temp_db):
    a = save_approval(5, 1, "fp", "u1")
    b = save_approval(5, 1, "fp", "u2")
    assert get_approval(a.approval_id).requested_by == "u1"
    # Latest for the plan is the most recent.
    assert get_latest_for_plan(5).approval_id == b.approval_id


def test_update_status_records_decision(temp_db):
    a = save_approval(1, 1, "fp", "alice")
    updated = update_status(
        a.approval_id, ApprovalStatus.APPROVED, decided_by="bob", decision_comment="ok"
    )
    assert updated.status == ApprovalStatus.APPROVED
    assert updated.decided_by == "bob"
    assert updated.decision_comment == "ok"
    assert updated.decision_time is not None


def test_list_and_summary(temp_db):
    a = save_approval(1, 1, "fp", "u")
    b = save_approval(2, 1, "fp", "u")
    update_status(b.approval_id, ApprovalStatus.APPROVED, decided_by="x")
    update_status(a.approval_id, ApprovalStatus.REJECTED, decided_by="y")
    assert len(list_approvals()) == 2
    summary = get_summary()
    assert summary.total == 2
    assert summary.approved == 1
    assert summary.rejected == 1


def test_get_unknown_returns_none(temp_db):
    assert get_approval(999) is None
    assert get_latest_for_plan(999) is None
    assert update_status(999, ApprovalStatus.APPROVED) is None
