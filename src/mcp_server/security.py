"""Boundary-enforcement helpers for guarded MCP tools.

None of the guarded tool schemas in ``src.mcp_server.server`` accept a
workspace id, subscription id, resource group, factory name, pipeline
name, or Fabric item id as a parameter — those identifiers always come
from ``Settings`` (environment / .env), never from the MCP caller. The
functions below are a defensive *second* line of checking: if a caller
somehow supplies one of these fields (e.g. a permissive client that adds
extra keys), it is validated against the single configured value and
rejected rather than silently used, exactly as required by Phase 12 §E.
"""

from __future__ import annotations

from typing import Any, Optional

from src.config import Settings


class BoundaryViolationError(Exception):
    """Raised when a caller-supplied identifier does not match the one
    configured value for that resource. Never invokes the service layer
    when raised."""

    def __init__(self, field: str):
        super().__init__(
            f"'{field}' does not match the configured environment boundary."
        )
        self.code = "BOUNDARY_VIOLATION"
        self.message = (
            f"The value supplied for '{field}' does not match the single "
            "configured environment identifier and was rejected before any "
            "service call was made."
        )
        self.field = field


def assert_matches_configured(
    field: str, supplied: Optional[Any], configured: Optional[Any]
) -> None:
    """Reject a caller-supplied identifier that does not exactly equal the
    configured value. A None/absent supplied value is always fine — it
    means the caller relied on configuration, which is the only supported
    path for every guarded tool's public schema."""
    if supplied is None:
        return
    if str(supplied) != str(configured or ""):
        raise BoundaryViolationError(field)


def assert_no_environment_overrides(settings: Settings, **maybe_supplied: Any) -> None:
    """Validate a bundle of optional caller-supplied identifiers (only ever
    present if a non-conforming client injects them) against the exactly
    configured Azure/Fabric boundary. Every field defaults to None because
    none of our tool schemas expose these parameters."""
    configured = {
        "subscription_id": settings.azure_subscription_id,
        "resource_group": settings.azure_resource_group,
        "data_factory_name": settings.azure_data_factory_name,
        "adf_source_pipeline_name": settings.adf_source_pipeline_name,
        "fabric_workspace_id": settings.fabric_workspace_id,
        "fabric_target_pipeline_item_id": settings.fabric_target_pipeline_item_id,
    }
    for field, supplied in maybe_supplied.items():
        if field not in configured:
            continue
        assert_matches_configured(field, supplied, configured[field])
