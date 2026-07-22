"""Advisory locking + idempotency helpers for guarded MCP operations.

Protects ``deploy_fabric_package``, ``run_source_pipeline``,
``run_fabric_pipeline``, and ``generate_report`` against duplicate
triggering. Two mechanisms:

1. **Advisory lock** — a unique ``(operation, lock_key)`` row in
   ``mcp_operation_locks`` acts as a mutex for the duration of one call.
   A second concurrent call for the same operation + resource key (e.g.
   the same plan id) fails the unique constraint and is rejected rather
   than silently racing the first call. The lock is released (row
   deleted) once the guarded operation finishes, success or failure.

2. **Idempotent replay** — before acquiring the lock, callers check
   whether a matching persisted result already exists (a REAL deployment
   for the same plan+approval+mode, a still-active execution for the
   same pipeline, or an already-written report for the same validation
   id) and return that existing result instead of repeating the
   underlying action. This is what makes retry-after-timeout and
   reconnect-after-MCP-restart safe: the check is a fresh database read
   every time, never an in-memory cache.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy.exc import IntegrityError

from src.database import McpOperationLockRecord, get_session_factory


class OperationInProgressError(Exception):
    """Raised when a concurrent call already holds the advisory lock."""

    def __init__(self, operation: str, lock_key: str):
        super().__init__(
            f"Operation '{operation}' is already in progress for '{lock_key}'."
        )
        self.operation = operation
        self.lock_key = lock_key
        self.code = "OPERATION_IN_PROGRESS"
        self.message = (
            f"Another call for {operation} on {lock_key} is already running; "
            "retry once it completes."
        )


@contextmanager
def advisory_lock(operation: str, lock_key: str, correlation_id: str) -> Iterator[None]:
    """Acquire a short-lived advisory lock for (operation, lock_key).

    Raises OperationInProgressError immediately (no blocking/waiting) if
    another call already holds it — guarded operations must never queue
    silently. Always releases the lock on exit.
    """
    session = get_session_factory()()
    try:
        record = McpOperationLockRecord(
            operation=operation, lock_key=str(lock_key), correlation_id=correlation_id
        )
        session.add(record)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise OperationInProgressError(operation, str(lock_key)) from None
    finally:
        session.close()

    try:
        yield
    finally:
        _release(operation, str(lock_key))


def _release(operation: str, lock_key: str) -> None:
    session = get_session_factory()()
    try:
        session.query(McpOperationLockRecord).filter(
            McpOperationLockRecord.operation == operation,
            McpOperationLockRecord.lock_key == lock_key,
        ).delete()
        session.commit()
    finally:
        session.close()


def find_existing_real_deployment(plan_id: int, approval_id: int) -> Optional[dict]:
    """Return the most recent REAL deployment for this exact plan+approval
    pair, if one has already run. Used so a duplicate deploy_fabric_package
    call for REAL mode never re-deploys — it returns the prior result.
    DRY_RUN/MOCK are safe to repeat and are never short-circuited here.
    """
    from src.migration.deployment_store import get_deployment, list_deployments

    for meta in list_deployments():
        if (
            meta["plan_id"] == plan_id
            and meta["approval_id"] == approval_id
            and meta["mode"] == "REAL"
        ):
            return get_deployment(meta["id"])
    return None


def find_existing_execution_by_correlation(correlation_id: str) -> Optional[dict]:
    """Return an existing execution result for this exact correlation id,
    if the caller already started one (idempotent retry key)."""
    from src.execution.execution_store import list_executions

    matches = list_executions(correlation_id=correlation_id)
    return matches[0].model_dump(mode="json") if matches else None
