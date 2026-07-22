"""Phase 10 REAL deployment integration tests (mocked Fabric HTTP)."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database as db_module
from src.approvals import approval_service as appr
from src.artifacts import write_package
from src.config import get_settings
from src.connectors.adf_source import FixtureADFSource
from src.database import Base
from src.migration.assessment import ADFCompatibilityAssessment
from src.migration.deployment import DeploymentService, FabricDeploymentDisabledError
from src.migration.discovery import ADFDiscoveryService
from src.migration.deployment_store import get_deployment
from src.migration.plan_store import save_plan
from src.migration.planner import MigrationPlanner
from src.models.schemas import (
    DeployableTargetType,
    DeploymentMode,
    DeploymentStatus,
    DeploymentStepStatus,
)
from tests import fabric_helpers as fh

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_module, "_engine", engine)
    monkeypatch.setattr(
        db_module, "_SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    gen = tmp_path / "generated"
    gen.mkdir()
    monkeypatch.setenv("GENERATED_ARTIFACTS_DIR", str(gen))
    get_settings.cache_clear()
    yield gen
    get_settings.cache_clear()


def _approved_plan(gen_dir, assessment_id=1):
    inv = FixtureADFSource(FIXTURES).load_inventory()
    result = ADFDiscoveryService(inv).scan_inventory()
    assessment = ADFCompatibilityAssessment(inv).assess_discovery(result)
    plan = MigrationPlanner(inv).generate_plan(result, assessment, 1)
    write_package(plan.generated_package, Path(gen_dir))
    rec = save_plan(plan, assessment_id=assessment_id)
    ap = appr.request_approval(rec["id"], "alice")
    appr.approve(ap.approval_id, "bob")
    return rec["id"], ap.approval_id


def _deploy_real(plan_id, approval_id, transport):
    svc = DeploymentService(fabric_client=fh.make_client(transport=transport))
    return svc.deploy(plan_id, approval_id, DeploymentMode.REAL)


# ── Gate + approval + package integrity ──────────────────────────


def test_real_disabled_by_default(env):
    plan_id, approval_id = _approved_plan(env)
    # No injected client + settings disabled -> hard stop.
    with pytest.raises(FabricDeploymentDisabledError):
        DeploymentService().deploy(plan_id, approval_id, DeploymentMode.REAL)


def test_missing_approval_blocks(env):
    plan_id, _ = _approved_plan(env)
    result = _deploy_real(plan_id, 999999, fh.FakeFabricTransport())
    assert result.status == DeploymentStatus.BLOCKED


def test_changed_package_blocks(env):
    plan_id, approval_id = _approved_plan(env)
    # Tamper a written artifact file after approval.
    artifact_file = next(Path(env).rglob("*.json"))
    artifact_file.write_text('{"tampered": true}', encoding="utf-8")
    result = _deploy_real(plan_id, approval_id, fh.FakeFabricTransport())
    assert result.status == DeploymentStatus.BLOCKED
    # Either the guard (PACKAGE_INVALID) or the explicit re-verification
    # (PACKAGE_VERIFICATION_FAILED) blocks the tampered package.
    assert "PACKAGE" in (result.error or "")


# ── Ordering, create, reuse, idempotency ─────────────────────────
#
# NOTE: the stock fixture's Mapping Data Flow has no real MDF -> Power
# Query conversion available (this codebase does not implement one), so
# its Dataflow Gen2 artifact is correctly NON_DEPLOYABLE. Because the
# Fabric Data Pipeline artifact depends on the dataflow (it invokes it),
# the pipeline and schedule are correctly SKIPPED after it — a full-chain
# REAL run against this fixture is honestly PARTIAL, not SUCCEEDED. See
# tests/test_phase10_corrected.py for a dataflow that DOES have a
# synthetic compiled Power Query mashup, proving the deployable path.


def test_dependency_order_and_create_success(env):
    plan_id, approval_id = _approved_plan(env)
    t = fh.FakeFabricTransport()
    result = _deploy_real(plan_id, approval_id, t)
    assert result.status == DeploymentStatus.PARTIAL
    types = [s.target_item_type for s in result.steps]
    # Connection first, schedule last (dependency order) still holds.
    assert types[0] == DeployableTargetType.CONNECTION.value
    assert types[-1] == DeployableTargetType.SCHEDULE.value

    by_type = {s.target_item_type: s for s in result.steps}
    assert by_type[DeployableTargetType.CONNECTION.value].status == DeploymentStepStatus.SUCCEEDED
    assert by_type[DeployableTargetType.LAKEHOUSE.value].status == DeploymentStepStatus.SUCCEEDED
    table_step = by_type[DeployableTargetType.LAKEHOUSE_TABLE.value]
    assert table_step.status == DeploymentStepStatus.SUCCEEDED
    assert getattr(table_step, "materialization_status", None) == "DEFERRED_TO_RUNTIME"
    assert table_step.resource_id is None

    dataflow_step = by_type[DeployableTargetType.DATAFLOW_GEN2.value]
    assert dataflow_step.status == DeploymentStepStatus.FAILED
    assert "FABRIC_ARTIFACT_NON_DEPLOYABLE" in (dataflow_step.error or "")

    assert by_type[DeployableTargetType.DATA_PIPELINE.value].status == DeploymentStepStatus.SKIPPED
    assert by_type[DeployableTargetType.SCHEDULE.value].status == DeploymentStepStatus.SKIPPED


def test_idempotent_rerun_reuses(env):
    plan_id, approval_id = _approved_plan(env)
    t = fh.FakeFabricTransport()
    first = _deploy_real(plan_id, approval_id, t)
    succeeded_ids = {
        s.artifact_id: s.resource_id
        for s in first.steps
        if s.status == DeploymentStepStatus.SUCCEEDED and s.resource_id
    }

    # Rerun against the SAME transport state (items now exist) -> reuse.
    second = _deploy_real(plan_id, approval_id, t)
    assert second.status == DeploymentStatus.PARTIAL
    reused_steps = {
        s.artifact_id: s
        for s in second.steps
        if s.status == DeploymentStepStatus.SUCCEEDED and s.resource_id
    }
    assert set(reused_steps) == set(succeeded_ids)
    assert all(getattr(s, "reused", False) is True for s in reused_steps.values())
    # No duplicate items: same ids returned.
    assert {k: v.resource_id for k, v in reused_steps.items()} == succeeded_ids


# ── Conflict / partial failure / retry ───────────────────────────


def test_conflicting_item_fails_safely(env):
    plan_id, approval_id = _approved_plan(env)
    t = fh.FakeFabricTransport()
    t.force = ("/items", 409)
    result = _deploy_real(plan_id, approval_id, t)
    assert result.status in (DeploymentStatus.FAILED, DeploymentStatus.PARTIAL)
    assert any(s.status == DeploymentStepStatus.FAILED for s in result.steps)


def test_partial_failure_stops_dependents_then_safe_retry(env):
    plan_id, approval_id = _approved_plan(env)
    t = fh.FakeFabricTransport()
    # Inject an HTTP-level failure on the Lakehouse (an earlier, genuinely
    # deployable artifact) — not the dataflow, which is NON_DEPLOYABLE for a
    # structural reason no retry can fix (see test above).
    t.fail_display_name = "lakehouse_migration_poc"

    partial = _deploy_real(plan_id, approval_id, t)
    assert partial.status == DeploymentStatus.PARTIAL
    by_type = {s.target_item_type: s for s in partial.steps}
    assert by_type[DeployableTargetType.LAKEHOUSE.value].status == DeploymentStepStatus.FAILED
    # Everything depending on the lakehouse (table, and transitively the
    # dataflow/pipeline/schedule chain) is stopped, not faked.
    assert by_type[DeployableTargetType.LAKEHOUSE_TABLE.value].status == DeploymentStepStatus.SKIPPED
    assert by_type[DeployableTargetType.DATAFLOW_GEN2.value].status == DeploymentStepStatus.SKIPPED

    # Fix the HTTP-level fault and retry: the lakehouse now succeeds.
    t.fail_display_name = None
    retry = _deploy_real(plan_id, approval_id, t)
    by_type_retry = {s.target_item_type: s for s in retry.steps}
    assert by_type_retry[DeployableTargetType.LAKEHOUSE.value].status == DeploymentStepStatus.SUCCEEDED
    assert by_type_retry[DeployableTargetType.LAKEHOUSE_TABLE.value].status == DeploymentStepStatus.SUCCEEDED
    # Overall stays PARTIAL: the dataflow is still structurally NON_DEPLOYABLE
    # and correctly blocks its dependents even after the unrelated fault is fixed.
    assert retry.status == DeploymentStatus.PARTIAL
    assert by_type_retry[DeployableTargetType.DATAFLOW_GEN2.value].status == DeploymentStepStatus.FAILED


# ── Boundary / safety ────────────────────────────────────────────


def test_cross_workspace_item_blocked(env):
    plan_id, approval_id = _approved_plan(env)
    t = fh.FakeFabricTransport()
    t.items = [{"id": "x", "type": "Lakehouse", "displayName": "y", "workspaceId": "other"}]
    result = _deploy_real(plan_id, approval_id, t)
    assert any(
        s.status == DeploymentStepStatus.FAILED and "BOUNDARY" in (s.error or "")
        for s in result.steps
    )


def test_no_delete_calls_and_results_persisted(env):
    plan_id, approval_id = _approved_plan(env)
    t = fh.FakeFabricTransport()
    result = _deploy_real(plan_id, approval_id, t)
    methods = {c[0] for c in t.calls}
    assert "DELETE" not in methods
    assert methods <= {"GET", "POST"}
    # Deployment result survives restart (reloaded from DB).
    reloaded = get_deployment(result.deployment_id)
    assert reloaded is not None
    assert reloaded["result"].status == DeploymentStatus.PARTIAL
    succeeded_with_item = [
        s for s in reloaded["result"].steps
        if s.status == DeploymentStepStatus.SUCCEEDED and s.target_item_type not in (
            DeployableTargetType.LAKEHOUSE_TABLE.value,
        )
    ]
    assert succeeded_with_item  # connection + lakehouse at least
    assert all(s.resource_id for s in succeeded_with_item)


def test_auth_failure_recorded(env):
    plan_id, approval_id = _approved_plan(env)
    svc = DeploymentService(
        fabric_client=fh.make_client(
            transport=fh.FakeFabricTransport(),
            token_provider=lambda: (_ for _ in ()).throw(RuntimeError("bad")),
        )
    )
    result = svc.deploy(plan_id, approval_id, DeploymentMode.REAL)
    assert result.status == DeploymentStatus.FAILED
    assert any("FABRIC_AUTH_FAILED" in (s.error or "") for s in result.steps)


# ── MOCK / DRY_RUN regression ────────────────────────────────────


def test_mock_and_dry_run_still_work(env):
    plan_id, approval_id = _approved_plan(env)
    dry = DeploymentService().deploy(plan_id, approval_id, DeploymentMode.DRY_RUN)
    assert dry.status == DeploymentStatus.SUCCEEDED
    assert all(s.resource_id is None for s in dry.steps)

    mock = DeploymentService().deploy(plan_id, approval_id, DeploymentMode.MOCK)
    assert mock.status == DeploymentStatus.SUCCEEDED
    assert all(s.resource_id for s in mock.steps)
