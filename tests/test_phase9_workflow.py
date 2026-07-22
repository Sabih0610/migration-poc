"""Phase 9 workflow integration — Azure discovery through the pipeline."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.api.routes as discovery_routes
import src.database as db_module
from src.connectors.azure_adf_source import AzureADFSource, redact_secret_values
from src.database import Base
from src.migration.assessment import ADFCompatibilityAssessment
from src.migration.discovery_runner import run_discovery
from src.migration.discovery_store import get_discovery, get_latest_discovery
from src.migration.plan_store import save_plan
from src.migration.planner import MigrationPlanner
from tests import azure_helpers as az


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(
        db_module, "_SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    yield engine


def _azure_source():
    return AzureADFSource(az.make_client())


def test_azure_discovery_persists_and_survives_restart(temp_db):
    record = run_discovery("azure", source=_azure_source())
    discovery_id = record["id"]

    # Simulate a restart: no process-local state, reload purely from DB.
    reloaded = get_discovery(discovery_id)
    assert reloaded is not None
    inv = reloaded["result"].inventory
    assert len(inv.pipelines) == 1
    assert inv.source_definitions["pipelines"][0]["name"] == "pl_sales_processing_legacy"
    # Dependencies present and dependency-first execution order exists.
    assert reloaded["result"].summary.dependency_count > 0


def test_assessment_plan_package_from_azure_snapshot(temp_db):
    record = run_discovery("azure", source=_azure_source())
    result = record["result"]

    assessment = ADFCompatibilityAssessment(result.inventory).assess_discovery(result)
    assert assessment.overall_status.value == "REQUIRES_CHANGE"

    plan = MigrationPlanner(result.inventory).generate_plan(
        result, assessment, record["id"]
    )
    assert plan.generated_package is not None
    assert len(plan.generated_package.artifacts) == 8
    assert len(plan.generated_package.manifest.entries) == 8
    # Plan is linked to the exact persisted snapshot.
    assert plan.discovery_id == record["id"]


def test_azure_matches_fixture_snapshot(temp_db):
    azure_rec = run_discovery("azure", source=_azure_source())
    fixture_rec = run_discovery("fixture")
    assert (
        azure_rec["result"].summary.model_dump()
        == fixture_rec["result"].summary.model_dump()
    )


def test_secret_values_redacted_before_persistence(temp_db):
    raw = az.fixture_raw_definitions()
    # Inject a linked-service secret into the raw Azure definition.
    raw["linked_services"][0]["properties"].setdefault("typeProperties", {})
    raw["linked_services"][0]["properties"]["typeProperties"]["connectionString"] = (
        "AccountKey=supersecretvalue==;Endpoint=x"
    )
    source = AzureADFSource(az.make_client(raw=raw))

    record = run_discovery("azure", source=source)  # must not raise
    stored = get_discovery(record["id"])["result"].inventory.source_definitions
    ls_props = stored["linked_services"][0]["properties"]["typeProperties"]
    assert ls_props["connectionString"] == "***REDACTED***"
    assert "supersecretvalue" not in str(stored)


def test_redact_preserves_structure():
    doc = {"typeProperties": {"url": "https://x", "connectionString": "secret=1"}}
    out = redact_secret_values(doc)
    assert out["typeProperties"]["url"] == "https://x"
    assert out["typeProperties"]["connectionString"] == "***REDACTED***"


# ── API mode selection ───────────────────────────────────────────


def test_scan_api_unknown_mode_400(temp_db):
    from fastapi.testclient import TestClient
    from src.api.app import app

    client = TestClient(app)
    assert client.post("/api/discovery/scan?mode=bogus").status_code == 400


def test_scan_api_azure_disabled_409(temp_db, monkeypatch):
    from fastapi.testclient import TestClient
    from src.api.app import app

    # Default settings: azure discovery disabled -> 409, no Azure contact.
    client = TestClient(app)
    resp = client.post("/api/discovery/scan?mode=azure")
    assert resp.status_code == 409


def test_scan_api_azure_success_with_mock(temp_db, monkeypatch):
    from fastapi.testclient import TestClient
    from src.api.app import app

    # Route calls run_discovery(mode) with no injected source; patch it to
    # use the fake-backed Azure source (never contacts Azure).
    import src.api.routes as routes

    monkeypatch.setattr(
        routes, "run_discovery",
        lambda mode: run_discovery(mode, source=_azure_source())
        if mode == "azure" else run_discovery(mode),
    )
    client = TestClient(app)
    resp = client.post("/api/discovery/scan?mode=azure")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "azure"
    assert body["summary"]["artifact_count"] > 0
