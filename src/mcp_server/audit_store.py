"""Audit persistence for MCP tool calls (Phase 12).

One row per tool invocation, written through the same SQLite database
and session-factory convention used by every other store in this
codebase (see ``src.database``). Reuses the existing recursive
redaction helper (``src.reports.report_service.redact_secrets``) so
credentials, tokens, and secret-shaped strings can never land in an
audit row, even by mistake.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from src.database import McpAuditLogRecord, get_session_factory
from src.mcp_server.envelope import bound_value
from src.reports.report_service import redact_secrets


def safe_input_summary(raw_input: Optional[dict]) -> str:
    """Redact + bound a tool's input arguments for safe audit storage."""
    if not raw_input:
        return "{}"
    redacted = redact_secrets(dict(raw_input))
    bounded = bound_value(redacted)
    try:
        return json.dumps(bounded, default=str)[:4000]
    except (TypeError, ValueError):
        return "***UNSERIALIZABLE***"


def record_audit(
    *,
    correlation_id: str,
    tool_name: str,
    permission_category: str,
    raw_input: Optional[dict],
    referenced_ids: Optional[dict],
    authorization_result: str,
    result_status: str,
    duration_ms: int,
    safe_error_category: Optional[str] = None,
) -> int:
    """Persist one audit row. Never raises past this boundary — audit
    persistence failures are logged by the caller but never block the
    tool response itself from reaching the client."""
    session = get_session_factory()()
    try:
        record = McpAuditLogRecord(
            correlation_id=correlation_id,
            tool_name=tool_name,
            permission_category=permission_category,
            safe_input_summary=safe_input_summary(raw_input),
            referenced_ids_json=json.dumps(referenced_ids or {}, default=str),
            authorization_result=authorization_result,
            result_status=result_status,
            duration_ms=int(duration_ms),
            safe_error_category=safe_error_category,
            created_at=datetime.now(timezone.utc),
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record.id
    finally:
        session.close()


def _to_dict(record: McpAuditLogRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "correlation_id": record.correlation_id,
        "tool_name": record.tool_name,
        "permission_category": record.permission_category,
        "safe_input_summary": record.safe_input_summary,
        "referenced_ids": json.loads(record.referenced_ids_json or "{}"),
        "authorization_result": record.authorization_result,
        "result_status": record.result_status,
        "duration_ms": record.duration_ms,
        "safe_error_category": record.safe_error_category,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def get_audit_record(audit_id: int) -> Optional[dict]:
    session = get_session_factory()()
    try:
        record = session.get(McpAuditLogRecord, audit_id)
        return _to_dict(record) if record else None
    finally:
        session.close()


def list_audit_records(
    *, tool_name: Optional[str] = None, correlation_id: Optional[str] = None, limit: int = 50
) -> list[dict]:
    session = get_session_factory()()
    try:
        query = session.query(McpAuditLogRecord)
        if tool_name is not None:
            query = query.filter(McpAuditLogRecord.tool_name == tool_name)
        if correlation_id is not None:
            query = query.filter(McpAuditLogRecord.correlation_id == correlation_id)
        records = query.order_by(McpAuditLogRecord.id.desc()).limit(limit).all()
        return [_to_dict(r) for r in records]
    finally:
        session.close()
