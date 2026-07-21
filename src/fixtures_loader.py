"""Fixture loader for ADF JSON and CSV files.

Loads mock ADF assets from the fixtures directory and returns
a validated ADFInventory. Also provides generic JSON and CSV
loading utilities with clean error handling.
"""

import csv
import json
import logging
from pathlib import Path
from typing import Optional

from src.models.schemas import (
    ADFInventory,
    ADFPipeline,
    Dataset,
    LinkedService,
    MappingDataFlow,
    Trigger,
)

logger = logging.getLogger(__name__)

# Keys that should never appear in fixture data
_CREDENTIAL_KEYS = {
    "password",
    "secret",
    "client_secret",
    "accountKey",
    "account_key",
    "connectionString",
    "connection_string",
    "accessToken",
    "access_token",
    "servicePrincipalKey",
    "service_principal_key",
}


def _check_no_credentials(data: dict, filepath: Path) -> None:
    """Recursively check that no credential-like keys exist in the data."""
    for key, value in data.items():
        if key.lower() in {k.lower() for k in _CREDENTIAL_KEYS}:
            raise ValueError(
                f"Credential-like key '{key}' found in {filepath}. "
                "Fixture files must not contain secrets."
            )
        if isinstance(value, dict):
            _check_no_credentials(value, filepath)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _check_no_credentials(item, filepath)


def load_json(path: Path) -> Optional[dict]:
    """Load a single JSON file and return as dict.

    Returns None and logs a warning on any error.
    """
    try:
        resolved = Path(path).resolve()
        if not resolved.exists():
            logger.warning("File not found: %s", resolved)
            return None
        text = resolved.read_text(encoding="utf-8")
        data = json.loads(text)
        _check_no_credentials(data, resolved)
        return data
    except json.JSONDecodeError as exc:
        logger.warning("Malformed JSON in %s: %s", path, exc)
        return None
    except ValueError as exc:
        logger.warning("Validation error in %s: %s", path, exc)
        raise
    except Exception as exc:
        logger.warning("Failed to load %s: %s", path, exc)
        return None


def load_csv(path: Path) -> Optional[list[dict]]:
    """Load a CSV file and return as list of row dicts.

    Validates that the file has headers. Returns None on error.
    """
    try:
        resolved = Path(path).resolve()
        if not resolved.exists():
            logger.warning("CSV file not found: %s", resolved)
            return None
        with open(resolved, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                logger.warning("CSV file %s has no headers.", resolved)
                return None
            rows = list(reader)
        return rows
    except Exception as exc:
        logger.warning("Failed to load CSV %s: %s", path, exc)
        return None


def _load_all_json_in(directory: Path) -> list[dict]:
    """Load all .json files in a directory."""
    results = []
    resolved = Path(directory).resolve()
    if not resolved.is_dir():
        logger.warning("Directory not found: %s", resolved)
        return results
    for json_file in sorted(resolved.glob("*.json")):
        data = load_json(json_file)
        if data is not None:
            results.append(data)
    return results


def load_mock_adf_inventory(fixtures_root: Path) -> ADFInventory:
    """Load a complete ADF inventory from the fixtures directory.

    Expected structure:
        fixtures_root/
            adf/
                linked_services/*.json
                datasets/*.json
                dataflows/*.json
                pipelines/*.json
                triggers/*.json

    Returns a validated ADFInventory model.
    Raises FileNotFoundError if required directories are missing.
    """
    root = Path(fixtures_root).resolve()
    adf_root = root / "adf"

    if not adf_root.is_dir():
        raise FileNotFoundError(
            f"ADF fixtures directory not found: {adf_root}"
        )

    # Required subdirectories
    required_dirs = [
        "linked_services",
        "datasets",
        "pipelines",
    ]
    for dirname in required_dirs:
        dirpath = adf_root / dirname
        if not dirpath.is_dir():
            raise FileNotFoundError(
                f"Required fixture directory missing: {dirpath}"
            )

    # Load each asset type
    linked_services_data = _load_all_json_in(adf_root / "linked_services")
    datasets_data = _load_all_json_in(adf_root / "datasets")
    dataflows_data = _load_all_json_in(adf_root / "dataflows")
    pipelines_data = _load_all_json_in(adf_root / "pipelines")
    triggers_data = _load_all_json_in(adf_root / "triggers")

    # Parse into models
    linked_services = [LinkedService(**d) for d in linked_services_data]
    datasets = [Dataset(**d) for d in datasets_data]
    data_flows = [MappingDataFlow(**d) for d in dataflows_data]
    pipelines = [ADFPipeline(**d) for d in pipelines_data]
    triggers = [Trigger(**d) for d in triggers_data]

    inventory = ADFInventory(
        pipelines=pipelines,
        linked_services=linked_services,
        datasets=datasets,
        data_flows=data_flows,
        triggers=triggers,
    )

    logger.info(
        "Loaded ADF inventory: %d pipelines, %d linked services, "
        "%d datasets, %d data flows, %d triggers.",
        len(pipelines),
        len(linked_services),
        len(datasets),
        len(data_flows),
        len(triggers),
    )

    return inventory
