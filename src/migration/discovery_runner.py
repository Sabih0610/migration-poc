"""Discovery runner — Phase 9.

Selects a discovery source (fixture or real read-only Azure), runs the
existing discovery engine, and persists the snapshot to discovery_runs.
Both modes produce the identical DiscoveryResult shape, so assessment,
planning, and package generation are unchanged.
"""

import logging
from pathlib import Path
from typing import Optional

from src.config import get_settings
from src.connectors.adf_source import ADFSource, FixtureADFSource
from src.connectors.azure_adf_source import build_azure_source_from_settings
from src.migration.discovery import ADFDiscoveryService
from src.migration.discovery_store import save_discovery

logger = logging.getLogger(__name__)

FIXTURES_ROOT = Path(__file__).resolve().parent.parent.parent / "fixtures"

FIXTURE_MODE = "fixture"
AZURE_MODE = "azure"
VALID_MODES = (FIXTURE_MODE, AZURE_MODE)


def build_source(
    mode: str,
    *,
    settings=None,
    fixtures_root: Optional[Path] = None,
) -> ADFSource:
    """Return the discovery source for a mode ('fixture' | 'azure')."""
    mode = (mode or FIXTURE_MODE).lower()
    if mode == FIXTURE_MODE:
        return FixtureADFSource(fixtures_root or FIXTURES_ROOT)
    if mode == AZURE_MODE:
        return build_azure_source_from_settings(settings or get_settings())
    raise ValueError(f"Unknown discovery mode '{mode}'. Use 'fixture' or 'azure'.")


def run_discovery(
    mode: str = FIXTURE_MODE,
    *,
    source: Optional[ADFSource] = None,
    settings=None,
    fixtures_root: Optional[Path] = None,
) -> dict:
    """Run discovery for the given mode and persist the snapshot.

    Returns the saved discovery record (id, counts, result).
    """
    src = source or build_source(mode, settings=settings, fixtures_root=fixtures_root)
    inventory = src.load_inventory()
    result = ADFDiscoveryService(inventory).scan_inventory()
    record = save_discovery(result)
    logger.info(
        "Discovery (%s) persisted as id=%d.", src.mode, record["id"]
    )
    return record
