"""Mock Microsoft Fabric connector — Phase 7.

Simulates Fabric item creation in memory. Makes NO network calls and
holds NO credentials. Creation is idempotent and returns deterministic
mock IDs. There are deliberately no delete methods.

`fail_on_action` injects a failure for a given action key so tests can
exercise error handling.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Action keys that create a tracked resource (vs. verify/run operations).
CREATION_KINDS = (
    "connection",
    "lakehouse",
    "table",
    "dataflow",
    "pipeline",
    "schedule",
)


class MockFabricError(Exception):
    """Raised by the mock client to simulate a Fabric failure."""


class MockFabricClient:
    """In-memory stand-in for the Fabric REST client."""

    def __init__(self, fail_on_action: Optional[str] = None):
        # (kind, name) -> mock id
        self._resources: dict[tuple, str] = {}
        self._workspaces: dict[str, str] = {}
        self._runs: dict[str, str] = {}
        # An action key (e.g. "create_table") that should raise when called.
        self.fail_on_action = fail_on_action

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _mock_id(kind: str, name: str) -> str:
        """Deterministic mock identifier."""
        return f"mock-{kind}-{name}"

    def _maybe_fail(self, action_key: str) -> None:
        if self.fail_on_action and self.fail_on_action == action_key:
            raise MockFabricError(f"Injected failure on '{action_key}'.")

    def _create(self, kind: str, name: str, action_key: str) -> str:
        self._maybe_fail(action_key)
        key = (kind, name)
        if key in self._resources:  # idempotent — no duplicate
            return self._resources[key]
        mock_id = self._mock_id(kind, name)
        self._resources[key] = mock_id
        logger.info("Mock created %s '%s' -> %s", kind, name, mock_id)
        return mock_id

    # ── Operations ───────────────────────────────────────────────

    def verify_workspace(self, name: str) -> str:
        self._maybe_fail("verify_workspace")
        mock_id = self._mock_id("workspace", name)
        self._workspaces[name] = mock_id
        return mock_id

    def create_connection(self, name: str) -> str:
        return self._create("connection", name, "create_connection")

    def create_lakehouse(self, name: str) -> str:
        return self._create("lakehouse", name, "create_lakehouse")

    def create_table(self, name: str) -> str:
        return self._create("table", name, "create_table")

    def create_dataflow(self, name: str) -> str:
        return self._create("dataflow", name, "create_dataflow")

    def create_pipeline(self, name: str) -> str:
        return self._create("pipeline", name, "create_pipeline")

    def configure_schedule(self, name: str) -> str:
        return self._create("schedule", name, "configure_schedule")

    def run_target(self, name: str) -> str:
        self._maybe_fail("run_target")
        run_id = self._mock_id("run", name)
        self._runs[name] = run_id
        return run_id

    # ── Introspection ────────────────────────────────────────────

    def resource_count(self) -> int:
        """Number of created resources (excludes verify/run operations)."""
        return len(self._resources)

    def has_resource(self, kind: str, name: str) -> bool:
        return (kind, name) in self._resources
