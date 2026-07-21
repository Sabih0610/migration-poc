"""Tests for Phase 4 assessment persistence (SQLite)."""

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import src.database as db_module
from src.database import Base
from src.migration.assessment_store import (
    get_assessment,
    get_latest_assessment,
    save_assessment,
)
from src.models.schemas import (
    AssessmentIssue,
    AssessmentResult,
    AssessmentStatus,
    AssetAssessment,
    IssueSeverity,
)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the database module at a throwaway SQLite file."""
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(db_module, "_SessionLocal", session_factory)
    yield engine


def _sample(status=AssessmentStatus.READY) -> AssessmentResult:
    issue = AssessmentIssue(
        rule_id="R1",
        asset_name="a",
        asset_type="dataset",
        status=status,
        severity=IssueSeverity.INFO,
        message="ok",
    )
    asset = AssetAssessment(
        asset_name="a", asset_type="dataset", status=status, issues=[issue]
    )
    return AssessmentResult(overall_status=status, assessments=[asset])


def test_table_created(temp_db):
    assert "assessment_runs" in inspect(temp_db).get_table_names()


def test_save_returns_id(temp_db):
    run_id = save_assessment(_sample())
    assert isinstance(run_id, int)
    assert run_id > 0


def test_save_and_load(temp_db):
    run_id = save_assessment(_sample(AssessmentStatus.REQUIRES_CHANGE))
    record = get_assessment(run_id)
    assert record is not None
    assert record["id"] == run_id
    assert record["overall_status"] == "REQUIRES_CHANGE"
    assert record["result"].overall_status == AssessmentStatus.REQUIRES_CHANGE
    assert len(record["result"].assessments) == 1


def test_get_latest(temp_db):
    save_assessment(_sample(AssessmentStatus.READY))
    second_id = save_assessment(_sample(AssessmentStatus.BLOCKED))
    latest = get_latest_assessment()
    assert latest is not None
    assert latest["id"] == second_id
    assert latest["overall_status"] == "BLOCKED"


def test_get_unknown_returns_none(temp_db):
    assert get_assessment(999999) is None


def test_get_latest_empty_returns_none(temp_db):
    assert get_latest_assessment() is None
