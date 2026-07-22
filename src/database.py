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


class DiscoveryRunRecord(Base):
    """Persisted, lossless source-definition discovery snapshot."""

    __tablename__ = "discovery_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    artifact_count = Column(Integer, nullable=False, default=0)
    component_count = Column(Integer, nullable=False, default=0)
    result_json = Column(Text, nullable=False)
    created_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<DiscoveryRunRecord(id={self.id}, "
            f"artifact_count={self.artifact_count}, "
            f"component_count={self.component_count})>"
        )


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


class ApprovalRequestRecord(Base):
    """Persisted migration-plan approval (Phase 6).

    Bound to a plan id, version, and fingerprint so any plan change
    invalidates a prior approval. Stores no secrets.
    """

    __tablename__ = "approval_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(Integer, nullable=False)
    plan_version = Column(Integer, nullable=False)
    plan_fingerprint = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="PENDING")
    requested_by = Column(String(255), nullable=False)
    decided_by = Column(String(255), nullable=True)
    request_comment = Column(Text, nullable=True)
    decision_comment = Column(Text, nullable=True)
    created_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    decided_at = Column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<ApprovalRequestRecord(id={self.id}, plan_id={self.plan_id}, "
            f"status={self.status!r})>"
        )


class DeploymentRunRecord(Base):
    """Persisted deployment run (Phase 7).

    Records a dry-run or mock deployment of an approved plan. Stores the
    serialized DeploymentResult as JSON. No secrets, no real Fabric IDs.
    """

    __tablename__ = "deployment_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(Integer, nullable=False)
    approval_id = Column(Integer, nullable=True)
    mode = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False)
    result_json = Column(Text, nullable=False)
    created_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at = Column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<DeploymentRunRecord(id={self.id}, plan_id={self.plan_id}, "
            f"mode={self.mode!r}, status={self.status!r})>"
        )

class ValidationRunRecord(Base):
    """Persisted validation run (Phase 8)."""
    __tablename__ = "validation_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    deployment_id = Column(Integer, nullable=False)
    plan_id = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False)
    result_json = Column(Text, nullable=False)
    created_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at = Column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<ValidationRunRecord(id={self.id}, deployment_id={self.deployment_id}, "
            f"status={self.status!r})>"
        )


class StructuralValidationRunRecord(Base):
    """Persisted artifact-definition structural validation run."""

    __tablename__ = "structural_validation_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    discovery_id = Column(Integer, nullable=False)
    deployment_id = Column(Integer, nullable=False)
    plan_id = Column(Integer, nullable=True)
    status = Column(String(32), nullable=False)
    result_json = Column(Text, nullable=False)
    created_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at = Column(DateTime, nullable=True)


class RuntimeValidationRunRecord(Base):
    """Persisted optional customer-runtime metric validation run."""

    __tablename__ = "runtime_validation_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    deployment_id = Column(Integer, nullable=False)
    plan_id = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False)
    result_json = Column(Text, nullable=False)
    created_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at = Column(DateTime, nullable=True)


class PipelineExecutionRecord(Base):
    """Persisted controlled source/target pipeline execution (Phase 11).

    Stores only safe run metadata — there is deliberately no free-form
    "data"/"payload" column, so customer row content can never be
    persisted through this table.
    """

    __tablename__ = "pipeline_executions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    correlation_id = Column(String(64), nullable=False, index=True)
    side = Column(String(16), nullable=False)  # "source" | "target"
    pipeline_identity = Column(String(255), nullable=False)
    run_id = Column(String(255), nullable=True)
    plan_id = Column(Integer, nullable=True)
    deployment_id = Column(Integer, nullable=True)
    discovery_snapshot_id = Column(Integer, nullable=True)
    status = Column(String(32), nullable=False)
    safe_error_category = Column(String(64), nullable=True)
    metrics_json = Column(Text, nullable=True)
    started_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Integer, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<PipelineExecutionRecord(id={self.id}, side={self.side!r}, "
            f"pipeline_identity={self.pipeline_identity!r}, status={self.status!r})>"
        )


class RuntimeExecutionValidationRecord(Base):
    """Persisted execution-linked runtime-equivalence validation (Phase 11).

    Distinct from ``runtime_validation_runs`` (Phase 8's metrics-only
    comparison): every row here references a real source and target
    pipeline execution, not just externally-supplied metrics.
    """

    __tablename__ = "runtime_execution_validations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(Integer, nullable=False)
    deployment_id = Column(Integer, nullable=False)
    correlation_id = Column(String(64), nullable=False, index=True)
    status = Column(String(32), nullable=False)
    result_json = Column(Text, nullable=False)
    created_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at = Column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<RuntimeExecutionValidationRecord(id={self.id}, "
            f"plan_id={self.plan_id}, status={self.status!r})>"
        )

class McpAuditLogRecord(Base):
    """Persisted audit row for one MCP tool call (Phase 12).

    One row per tool invocation. Never stores credentials, tokens, raw
    SDK exceptions, customer row content, or unbounded definitions — only
    a bounded, redacted summary of the call. Survives process restart:
    same SQLite database file used by every other store in this codebase.
    """

    __tablename__ = "mcp_audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    correlation_id = Column(String(64), nullable=False, index=True)
    tool_name = Column(String(128), nullable=False, index=True)
    permission_category = Column(String(32), nullable=False)
    safe_input_summary = Column(Text, nullable=True)
    referenced_ids_json = Column(Text, nullable=True)
    authorization_result = Column(String(32), nullable=False)
    result_status = Column(String(32), nullable=False)
    duration_ms = Column(Integer, nullable=True)
    safe_error_category = Column(String(64), nullable=True)
    created_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<McpAuditLogRecord(id={self.id}, tool_name={self.tool_name!r}, "
            f"result_status={self.result_status!r})>"
        )


class McpOperationLockRecord(Base):
    """Advisory concurrency lock for guarded/state-changing MCP operations
    (Phase 12). A unique (operation, lock_key) row acts as a mutex: a
    second concurrent call for the same operation + resource key fails
    the unique constraint and is rejected as "already in progress" rather
    than silently duplicating the underlying action. Rows are deleted
    once the guarded operation completes (success or failure).
    """

    __tablename__ = "mcp_operation_locks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    operation = Column(String(64), nullable=False)
    lock_key = Column(String(128), nullable=False)
    correlation_id = Column(String(64), nullable=False)
    created_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("operation", "lock_key", name="uq_mcp_operation_lock"),
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
