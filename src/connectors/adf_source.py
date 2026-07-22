"""ADF discovery source boundary — Phase 9.

Defines the common interface used by both fixture discovery (offline,
default) and real read-only Azure discovery, so downstream code can treat
them interchangeably. Fixture discovery remains the default everywhere.
"""

from abc import ABC, abstractmethod
from pathlib import Path

from src.fixtures_loader import load_mock_adf_inventory
from src.models.schemas import ADFInventory


class ADFDiscoveryError(Exception):
    """Base error for discovery-source failures. Carries a stable code.

    The message is always safe to surface to callers/logs: it never
    contains credentials, tokens, or raw SDK error text.
    """

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code


class ADFSource(ABC):
    """A source that can produce an ADFInventory."""

    #: short mode identifier ("fixture" | "azure")
    mode: str = "unknown"

    @abstractmethod
    def load_inventory(self) -> ADFInventory:
        """Return a validated ADFInventory of source assets."""

    def describe(self) -> dict:
        """Return safe, non-sensitive metadata about this source."""
        return {"mode": self.mode}


class FixtureADFSource(ADFSource):
    """Loads the mock ADF inventory from local fixture files (offline)."""

    mode = "fixture"

    def __init__(self, fixtures_root: Path):
        self.fixtures_root = Path(fixtures_root)

    def load_inventory(self) -> ADFInventory:
        return load_mock_adf_inventory(self.fixtures_root)

    def describe(self) -> dict:
        return {"mode": self.mode, "fixtures_root": str(self.fixtures_root)}
