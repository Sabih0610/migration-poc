"""SQLite database foundation using SQLAlchemy.

Creates engine, session factory, and the app_metadata table.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from src.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


class AppMetadata(Base):
    """Key-value metadata table for application state."""

    __tablename__ = "app_metadata"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(255), nullable=False)
    value = Column(Text, nullable=True)
    created_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (UniqueConstraint("key", name="uq_app_metadata_key"),)

    def __repr__(self) -> str:
        return f"<AppMetadata(key={self.key!r}, value={self.value!r})>"


class AssessmentRun(Base):
    """Persisted compatibility-assessment run (Phase 4).

    Stores the serialized AssessmentResult as JSON. Never stores
    credentials — the result models carry none.
    """

    __tablename__ = "assessment_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    overall_status = Column(String(32), nullable=False)
    result_json = Column(Text, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<AssessmentRun(id={self.id}, "
            f"overall_status={self.overall_status!r})>"
        )


class MigrationPlanRecord(Base):
    """Persisted migration plan (Phase 5).

    Versioned per assessment. Stores the serialized MigrationPlan as
    JSON. Never stores credentials — the plan models carry none.
    """

    __tablename__ = "migration_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    assessment_id = Column(Integer, nullable=True)
    version = Column(Integer, nullable=False, default=1)
    executable = Column(Boolean, nullable=False, default=True)
    overall_risk = Column(String(32), nullable=False)
    plan_json = Column(Text, nullable=False)
    created_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<MigrationPlanRecord(id={self.id}, "
            f"assessment_id={self.assessment_id}, version={self.version})>"
        )


# ── Engine & Session ─────────────────────────────────────────────

_engine = None
_SessionLocal = None


def get_engine():
    """Return the SQLAlchemy engine (created on first call)."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
            echo=False,
        )
    return _engine


def get_session_factory():
    """Return the session factory (created on first call)."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=get_engine()
        )
    return _SessionLocal


def init_database() -> None:
    """Create all tables. Safe to call multiple times."""
    try:
        engine = get_engine()
        Base.metadata.create_all(bind=engine)
        logger.info("Database initialized successfully.")
    except Exception as exc:
        logger.error("Failed to initialize database: %s", exc)
        raise


def get_db():
    """Dependency that yields a database session."""
    session_factory = get_session_factory()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()
