"""Tests for Phase 5 migration plan persistence (SQLite)."""

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import src.database as db_module
from src.database import Base
from src.migration.plan_store import (
    get_latest_plan,
    get_plan,
    list_plans,
    save_plan,
)
from src.models.schemas import MigrationPlan, MigrationRisk


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


def _plan(executable=True, risk=MigrationRisk.MEDIUM) -> MigrationPlan:
    return MigrationPlan(executable=executable, overall_risk=risk)


def test_table_created(temp_db):
    assert "migration_plans" in inspect(temp_db).get_table_names()


def test_save_returns_record_with_version_one(temp_db):
    record = save_plan(_plan(), assessment_id=1)
    assert record["id"] > 0
    assert record["version"] == 1
    assert record["overall_risk"] == "MEDIUM"


def test_version_increments_for_same_assessment(temp_db):
    r1 = save_plan(_plan(), assessment_id=7)
    r2 = save_plan(_plan(), assessment_id=7)
    r3 = save_plan(_plan(), assessment_id=7)
    assert [r1["version"], r2["version"], r3["version"]] == [1, 2, 3]


def test_version_independent_per_assessment(temp_db):
    a = save_plan(_plan(), assessment_id=1)
    b = save_plan(_plan(), assessment_id=2)
    assert a["version"] == 1
    assert b["version"] == 1


def test_get_plan_round_trips(temp_db):
    saved = save_plan(_plan(executable=False, risk=MigrationRisk.CRITICAL), assessment_id=3)
    loaded = get_plan(saved["id"])
    assert loaded is not None
    assert loaded["executable"] is False
    assert loaded["overall_risk"] == "CRITICAL"
    assert isinstance(loaded["plan"], MigrationPlan)
    assert loaded["plan"].overall_risk == MigrationRisk.CRITICAL


def test_get_latest_plan(temp_db):
    save_plan(_plan(), assessment_id=1)
    second = save_plan(_plan(risk=MigrationRisk.HIGH), assessment_id=1)
    latest = get_latest_plan()
    assert latest["id"] == second["id"]
    assert latest["overall_risk"] == "HIGH"


def test_list_plans(temp_db):
    save_plan(_plan(), assessment_id=1)
    save_plan(_plan(), assessment_id=1)
    plans = list_plans()
    assert len(plans) == 2
    # Newest first, and no plan body in the listing.
    assert plans[0]["id"] > plans[1]["id"]
    assert "plan" not in plans[0]


def test_get_unknown_returns_none(temp_db):
    assert get_plan(999999) is None


def test_get_latest_empty_returns_none(temp_db):
    assert get_latest_plan() is None
