"""Phase 12 — local Python STDIO MCP server package.

This package is purely a controlled *interface* layer over the existing
deterministic Python migration services (``src.migration``, ``src.approvals``,
``src.validation``, ``src.reports``, ``src.connectors``). It never
reimplements discovery, assessment, planning, approval, deployment,
validation, or reporting business logic — every tool handler in
``src.mcp_server.handlers`` calls directly into the existing service layer.

Transport is local STDIO only (see ``src.mcp_server.server``); there is no
HTTP listener here and the FastAPI app in ``src.api.app`` is unaffected.
"""
