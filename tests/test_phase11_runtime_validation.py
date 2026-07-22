"""Phase 11 runtime-equivalence validation tests.

Covers rule semantics (PASS / PASS_WITH_WARNINGS / FAIL / INCONCLUSIVE),
the structural/runtime separation invariant, and reporting appendix
generation. All Azure/Fabric access is mocked — zero real cloud calls.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database as db_module
from src.config import get_settings
from src.database import Base, StructuralValidationRunRecord, get_session_factory
from src.execution.execution_store import complete_execution, start_execution
from src.models.schemas import (
    ExecutionSide,
    ExecutionStatus,
    RuntimeMetrics,
    RuntimeValidationRuleConfig,
    RuntimeValidationStatus,
)
from src.reports.report_service import generate_report
from src.validation.runtime_execution_validation_store import (
    save_runtime_execution_validation,
)
from src.validation.runtime_validation_service import (
    RuntimeValidationError,
    RuntimeValidationService,
)
from src.validation.structural_store import get_structural_validation
from tests import execution_helpers as eh
from tests import fabric_helpers as fh


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
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setenv("GENERATED_ARTIFACTS_DIR", str(gen))
    monkeypatch.setenv("REPORTS_DIR", str(reports))
    get_settings.cache_clear()
    yield gen
    get_settings.cache_clear()


def _enable_runtime_execution(monkeypatch, pipeline_item_id: str):
    for key, value in {
        "RUNTIME_EXECUTION_ENABLED": "true",
        "AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "c", "AZURE_CLIENT_SECRET": "s",
        "AZURE_SUBSCRIPTION_ID": "11111111-1111-1111-1111-111111111111",
        "AZURE_RESOURCE_GROUP": "AzureFabricMigrationPOC",
        "AZURE_DATA_FACTORY_NAME": "Sabih-df",
        "ADF_SOURCE_PIPELINE_NAME": "pl_sales_processing_legacy",
        "FABRIC_TENANT_ID": "t", "FABRIC_CLIENT_ID": "c", "FABRIC_CLIENT_SECRET": "s",
        "FABRIC_WORKSPACE_ID": fh.WS,
        "FABRIC_TARGET_PIPELINE_ITEM_ID": pipeline_item_id,
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _build_context(gen_dir, monkeypatch):
    """Build an approved plan, REAL-deploy it, structurally-PASS it, and
    return (plan_id, deployment_id, item_id).

    Uses the synthetic single-Data-Pipeline plan from
    ``tests.execution_helpers`` rather than the full ADF fixture set: the
    fixture's Mapping Data Flow has no real MDF -> Power Query converter
    anywhere in this codebase, so its Dataflow Gen2 artifact is correctly
    NON_DEPLOYABLE and the dependent Data Pipeline is correctly SKIPPED
    (see scripts/verify_phase10_deployment.py) — it never gets a real
    resource id, which this test's authorization flow requires.
    """
    plan_id, approval_id, real, item_id, _transport = eh.build_real_pipeline_deployment(
        gen_dir
    )
    _enable_runtime_execution(monkeypatch, item_id)
    return plan_id, real.deployment_id, item_id


def _make_executions(plan_id, deployment_id, item_id, source_metrics, target_metrics,
                      source_status=ExecutionStatus.SUCCEEDED, target_status=ExecutionStatus.SUCCEEDED):
    source = start_execution(
        correlation_id="corr-1", side=ExecutionSide.SOURCE,
        pipeline_identity="pl_sales_processing_legacy", plan_id=plan_id,
    )
    source = complete_execution(
        source.execution_id, status=source_status, run_id="adf-run-1",
        metrics=source_metrics,
    )
    target = start_execution(
        correlation_id="corr-1", side=ExecutionSide.TARGET,
        pipeline_identity=item_id, plan_id=plan_id, deployment_id=deployment_id,
    )
    target = complete_execution(
        target.execution_id, status=target_status, run_id="job-1",
        metrics=target_metrics,
    )
    return source, target


def _metrics(**overrides):
    base = dict(
        status="Succeeded",
        schemas={"order_id": "string", "gross_amount": "decimal"},
        total_row_count=20, valid_row_count=18, rejected_row_count=2,
        numeric_totals={"gross_amount": 5000.0},
        grouped_totals={"region": {"North": 2000.0, "South": 2800.0}},
        null_counts={"region": 0}, duplicate_counts={"order_id": 0},
        duration_seconds=120.0,
    )
    base.update(overrides)
    return RuntimeMetrics(**base)


# ── Disabled by default / gating ──────────────────────────────────


def test_runtime_validation_service_never_invents_missing_deployment(env):
    with pytest.raises(RuntimeValidationError):
        RuntimeValidationService().validate(999999, 999998)


# ── Exact match / mismatch / tolerance / missing ──────────────────


def test_exact_match_passes(env, monkeypatch):
    plan_id, deployment_id, item_id = _build_context(env, monkeypatch)
    source, target = _make_executions(
        plan_id, deployment_id, item_id, _metrics(), _metrics()
    )
    result = RuntimeValidationService().validate(source.execution_id, target.execution_id)
    assert result.status == RuntimeValidationStatus.PASS
    assert result.plan_id == plan_id
    assert result.deployment_id == deployment_id
    assert result.source_execution_id == source.execution_id
    assert result.target_execution_id == target.execution_id
    assert result.source_run_id == "adf-run-1"
    assert result.target_run_id == "job-1"
    assert result.correlation_id == source.correlation_id


def test_row_count_mismatch_fails(env, monkeypatch):
    plan_id, deployment_id, item_id = _build_context(env, monkeypatch)
    source, target = _make_executions(
        plan_id, deployment_id, item_id,
        _metrics(total_row_count=20), _metrics(total_row_count=15),
    )
    result = RuntimeValidationService().validate(source.execution_id, target.execution_id)
    assert result.status == RuntimeValidationStatus.FAIL
    row_check = next(c for c in result.checks if c.name == "total_row_count")
    assert row_check.status == RuntimeValidationStatus.FAIL
    assert row_check.explanation


def test_schema_mismatch_fails(env, monkeypatch):
    plan_id, deployment_id, item_id = _build_context(env, monkeypatch)
    source, target = _make_executions(
        plan_id, deployment_id, item_id,
        _metrics(schemas={"order_id": "string"}),
        _metrics(schemas={"order_id": "int"}),
    )
    result = RuntimeValidationService().validate(source.execution_id, target.execution_id)
    assert result.status == RuntimeValidationStatus.FAIL
    schema_check = next(c for c in result.checks if c.name == "schema")
    assert schema_check.status == RuntimeValidationStatus.FAIL


def test_numeric_total_mismatch_fails(env, monkeypatch):
    plan_id, deployment_id, item_id = _build_context(env, monkeypatch)
    source, target = _make_executions(
        plan_id, deployment_id, item_id,
        _metrics(numeric_totals={"gross_amount": 5000.0}),
        _metrics(numeric_totals={"gross_amount": 4000.0}),
    )
    result = RuntimeValidationService().validate(source.execution_id, target.execution_id)
    assert result.status == RuntimeValidationStatus.FAIL
    assert any(c.name.startswith("numeric_total:") and c.status == RuntimeValidationStatus.FAIL
               for c in result.checks)


def test_grouped_total_mismatch_fails(env, monkeypatch):
    plan_id, deployment_id, item_id = _build_context(env, monkeypatch)
    source, target = _make_executions(
        plan_id, deployment_id, item_id,
        _metrics(grouped_totals={"region": {"North": 2000.0, "South": 2800.0}}),
        _metrics(grouped_totals={"region": {"North": 1000.0, "South": 2800.0}}),
    )
    result = RuntimeValidationService().validate(source.execution_id, target.execution_id)
    assert result.status == RuntimeValidationStatus.FAIL
    assert any(c.name.startswith("grouped_total:") and c.status == RuntimeValidationStatus.FAIL
               for c in result.checks)


def test_within_tolerance_passes(env, monkeypatch):
    plan_id, deployment_id, item_id = _build_context(env, monkeypatch)
    rules = RuntimeValidationRuleConfig(row_count_tolerance=2, numeric_total_tolerance=10.0)
    source, target = _make_executions(
        plan_id, deployment_id, item_id,
        _metrics(total_row_count=20, numeric_totals={"gross_amount": 5000.0}),
        _metrics(total_row_count=21, numeric_totals={"gross_amount": 5005.0}),
    )
    result = RuntimeValidationService().validate(
        source.execution_id, target.execution_id, rules=rules
    )
    assert result.status == RuntimeValidationStatus.PASS


def test_duration_only_deviation_is_warning_not_failure(env, monkeypatch):
    plan_id, deployment_id, item_id = _build_context(env, monkeypatch)
    source, target = _make_executions(
        plan_id, deployment_id, item_id,
        _metrics(duration_seconds=100.0), _metrics(duration_seconds=250.0),
    )
    result = RuntimeValidationService().validate(source.execution_id, target.execution_id)
    assert result.status == RuntimeValidationStatus.PASS_WITH_WARNINGS
    duration_check = next(c for c in result.checks if c.name == "duration_seconds")
    assert duration_check.status == RuntimeValidationStatus.PASS_WITH_WARNINGS


def test_missing_metrics_never_silently_pass(env, monkeypatch):
    plan_id, deployment_id, item_id = _build_context(env, monkeypatch)
    incomplete = _metrics(numeric_totals={})
    source, target = _make_executions(
        plan_id, deployment_id, item_id, incomplete, _metrics(),
    )
    result = RuntimeValidationService().validate(source.execution_id, target.execution_id)
    # Missing metric -> INCONCLUSIVE, never a silent PASS for that check.
    numeric_checks = [c for c in result.checks if c.name.startswith("numeric_total")]
    assert numeric_checks and all(
        c.status == RuntimeValidationStatus.INCONCLUSIVE for c in numeric_checks
    )
    assert result.status == RuntimeValidationStatus.INCONCLUSIVE


def test_source_execution_failure_forces_fail(env, monkeypatch):
    plan_id, deployment_id, item_id = _build_context(env, monkeypatch)
    source, target = _make_executions(
        plan_id, deployment_id, item_id, _metrics(), _metrics(),
        source_status=ExecutionStatus.FAILED,
    )
    result = RuntimeValidationService().validate(source.execution_id, target.execution_id)
    assert result.status == RuntimeValidationStatus.FAIL


def test_target_execution_failure_forces_fail(env, monkeypatch):
    plan_id, deployment_id, item_id = _build_context(env, monkeypatch)
    source, target = _make_executions(
        plan_id, deployment_id, item_id, _metrics(), _metrics(),
        target_status=ExecutionStatus.FAILED,
    )
    result = RuntimeValidationService().validate(source.execution_id, target.execution_id)
    assert result.status == RuntimeValidationStatus.FAIL


# ── Structural / runtime separation invariant ─────────────────────


def _structural_row_bytes(deployment_id):
    session = get_session_factory()()
    try:
        record = (
            session.query(StructuralValidationRunRecord)
            .filter(StructuralValidationRunRecord.deployment_id == deployment_id)
            .order_by(StructuralValidationRunRecord.id.desc())
            .first()
        )
        return record.result_json.encode("utf-8") if record else None
    finally:
        session.close()


def test_structural_validation_row_unchanged_by_runtime_pass_and_fail(env, monkeypatch):
    plan_id, deployment_id, item_id = _build_context(env, monkeypatch)
    # NOTE: structural validation is recorded against the MOCK deployment,
    # not the REAL deployment used for runtime execution — grab its id.
    from src.validation.structural_store import get_latest_structural_validation
    structural_before = get_latest_structural_validation()
    before_bytes = _structural_row_bytes(structural_before.deployment_id)
    assert before_bytes is not None

    # Runtime PASS.
    source, target = _make_executions(plan_id, deployment_id, item_id, _metrics(), _metrics())
    result_pass = RuntimeValidationService().validate(source.execution_id, target.execution_id)
    assert result_pass.status == RuntimeValidationStatus.PASS
    after_pass_bytes = _structural_row_bytes(structural_before.deployment_id)
    assert after_pass_bytes == before_bytes

    # Runtime FAIL (mismatched row counts) — still must not touch structural row.
    source2, target2 = _make_executions(
        plan_id, deployment_id, item_id,
        _metrics(total_row_count=20), _metrics(total_row_count=1),
    )
    result_fail = RuntimeValidationService().validate(source2.execution_id, target2.execution_id)
    assert result_fail.status == RuntimeValidationStatus.FAIL
    after_fail_bytes = _structural_row_bytes(structural_before.deployment_id)
    assert after_fail_bytes == before_bytes

    # Structural status itself is unaffected and still queryable/unchanged.
    structural_after = get_structural_validation(structural_before.validation_id)
    assert structural_after.status == structural_before.status
    assert structural_after.model_dump_json() == structural_before.model_dump_json()


def test_structural_pass_and_runtime_fail_coexist(env, monkeypatch):
    plan_id, deployment_id, item_id = _build_context(env, monkeypatch)
    from src.validation.structural_store import get_latest_structural_validation
    structural = get_latest_structural_validation()
    assert structural.status.value in ("PASSED", "PASSED_WITH_WARNINGS")

    source, target = _make_executions(
        plan_id, deployment_id, item_id,
        _metrics(total_row_count=20), _metrics(total_row_count=1),
    )
    runtime = RuntimeValidationService().validate(source.execution_id, target.execution_id)
    assert runtime.status == RuntimeValidationStatus.FAIL
    # Both facts hold simultaneously without contradiction.
    structural_again = get_structural_validation(structural.validation_id)
    assert structural_again.status == structural.status


# ── No raw-row content in persisted records ────────────────────────


def test_no_raw_row_content_in_runtime_validation_record(env, monkeypatch):
    plan_id, deployment_id, item_id = _build_context(env, monkeypatch)
    source, target = _make_executions(plan_id, deployment_id, item_id, _metrics(), _metrics())
    result = RuntimeValidationService().validate(source.execution_id, target.execution_id)
    saved = save_runtime_execution_validation(result)
    dumped = saved.model_dump_json().lower()
    for forbidden in ('"data"', '"rows"', '"row_data"', '"records"'):
        assert forbidden not in dumped


# ── Reporting appendix ─────────────────────────────────────────────


def test_report_includes_optional_runtime_appendix(env, monkeypatch):
    plan_id, deployment_id, item_id = _build_context(env, monkeypatch)
    from src.validation.structural_store import get_latest_structural_validation
    structural = get_latest_structural_validation()

    source, target = _make_executions(plan_id, deployment_id, item_id, _metrics(), _metrics())
    runtime = RuntimeValidationService().validate(source.execution_id, target.execution_id)
    save_runtime_execution_validation(runtime)

    report = generate_report(structural.validation_id)
    assert report.runtime_execution_validation is not None
    assert report.runtime_execution_validation["status"] == "PASS"

    html_path = report.report_id  # unused; just ensure generation didn't raise
    from src.reports.report_service import report_path
    html = report_path(structural.validation_id, "html").read_text(encoding="utf-8")
    assert "runtime-equivalence validation" in html.lower()
    assert "never alters the structural validation status" in html.lower()


def test_report_omits_runtime_appendix_when_absent(env, monkeypatch):
    plan_id, deployment_id, item_id = _build_context(env, monkeypatch)
    from src.validation.structural_store import get_latest_structural_validation
    structural = get_latest_structural_validation()
    report = generate_report(structural.validation_id)
    assert report.runtime_execution_validation is None
