"""Shared MCP tool-result envelope, output bounding, and safe error mapping.

Every tool response goes through :func:`build_envelope` so the shape is
always exactly the same — this module contains the *only* place that
constructs that shape. No tool handler hand-rolls its own response dict.
"""

from __future__ import annotations

from typing import Any, Optional

from src.reports.report_service import redact_secrets

# ── Output bounding ──────────────────────────────────────────────

MAX_LIST_ITEMS = 50
MAX_STRING_LEN = 4000
MAX_DEPTH = 12


def bound_value(value: Any, *, depth: int = 0) -> Any:
    """Recursively cap list length and string length so no tool response
    can return an unbounded blob. Never mutates the input."""
    if depth >= MAX_DEPTH:
        return "***TRUNCATED (max depth)***"
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): bound_value(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [bound_value(v, depth=depth + 1) for v in list(value)[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            items.append(
                f"***TRUNCATED: {len(value) - MAX_LIST_ITEMS} more item(s) omitted***"
            )
        return items
    if isinstance(value, str) and len(value) > MAX_STRING_LEN:
        return value[:MAX_STRING_LEN] + f"...***TRUNCATED ({len(value)} chars)***"
    return value


def safe_output(data: Any) -> dict:
    """Redact secrets then bound size. Always returns a dict."""
    redacted = redact_secrets(data if data is not None else {})
    bounded = bound_value(redacted)
    if not isinstance(bounded, dict):
        bounded = {"value": bounded}
    return bounded


# ── Safe exception -> sanitized error code mapping ──────────────

# Exceptions across the existing service layer already carry a stable
# ``.code`` + ``.message`` pair (AzureDiscoveryError, FabricError,
# ApprovalError, DeploymentAuthorizationError, ExecutionAuthorizationError,
# AzureExecutionError, RuntimeValidationError, DuplicateExecutionError-like).
# We reuse that convention rather than inventing a second one.


def map_exception(exc: BaseException) -> tuple[str, str]:
    """Return (safe_error_category, safe_message) for any exception raised
    by the service layer. Never leaks raw SDK text or stack content."""
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None)
    if code:
        return str(code), str(message or exc) if message or str(exc) else str(code)
    # Known non-coded exceptions from the existing service layer.
    type_name = type(exc).__name__
    _KNOWN = {
        "ValueError": "INVALID_INPUT",
        "TypeError": "INVALID_INPUT",
        "FileNotFoundError": "RESOURCE_NOT_FOUND",
        "ArtifactPackageError": "PACKAGE_INVALID",
        "StructuralValidationError": "STRUCTURAL_VALIDATION_NOT_ELIGIBLE",
        "RuntimeError": "OPERATION_FAILED",
    }
    if type_name in _KNOWN:
        return _KNOWN[type_name], str(exc)
    # Unknown/unexpected — never surface raw exception detail.
    return "INTERNAL_ERROR", "An internal error occurred."


def build_envelope(
    *,
    success: bool,
    operation: str,
    status: str,
    correlation_id: str,
    permission_category: str,
    data: Optional[Any] = None,
    warnings: Optional[list[str]] = None,
    errors: Optional[list[str]] = None,
    approval_required: bool = False,
    next_allowed_actions: Optional[list[str]] = None,
) -> dict:
    """Build the single, consistent MCP tool-result envelope shape."""
    return {
        "success": bool(success),
        "operation": operation,
        "status": status,
        "data": safe_output(data),
        "warnings": list(warnings or []),
        "errors": list(errors or []),
        "correlation_id": correlation_id,
        "permission_category": permission_category,
        "approval_required": bool(approval_required),
        "next_allowed_actions": list(next_allowed_actions or []),
    }
