"""Phase 11 runtime-equivalence validation verification (fully mocked).

Exercises: metrics collection stub (mock metrics provider), rule
evaluation across PASS / PASS_WITH_WARNINGS / FAIL / INCONCLUSIVE,
the structural-validation-unchanged proof, and reporting appendix
generation. Zero real Azure/Fabric calls are made.

Exit 0 = PASS, non-zero = FAIL.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_helper import TempDatabase
from src.approvals import approval_service as appr
from src.artifacts import write_package
from src.config import get_settings
from src.connectors.adf_source import FixtureADFSource
from src.database import StructuralValidationRunRecord, get_session_factory
from src.execution.execution_store import complete_execution, start_execution
from src.migration.assessment import ADFCompatibilityAssessment
from src.migration.deployment import DeploymentService
from src.migration.discovery import ADFDiscoveryService
from src.migration.plan_store import save_plan
from src.migration.planner import MigrationPlanner
from src.models.schemas import (
    DeployableTargetType,
    DeploymentMode,
    ExecutionSide,
    ExecutionStatus,
    RuntimeMetrics,
    RuntimeValidationRuleConfig,
    RuntimeValidationStatus,
)
from src.reports.report_service import generate_report, report_path
from src.validation.runtime_execution_validation_store import save_runtime_execution_validation
from src.validation.runtime_validation_service import RuntimeValidationService
from src.validation.structural_store import (
    get_latest_structural_validation,
    get_structural_validation,
    save_structural_validation,
)
from src.validation.structural_validator import StructuralValidationService
from tests import fabric_helpers as fh

FIXTURES = PROJECT_ROOT / "fixtures"


def _enable(item_id):
    import os
    for key, value in {
        "RUNTIME_EXECUTION_ENABLED": "true",
        "AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "c", "AZURE_CLIENT_SECRET": "s",
        "AZURE_SUBSCRIPTION_ID": "11111111-1111-1111-1111-111111111111",
        "AZURE_RESOURCE_GROUP": "AzureFabricMigrationPOC",
        "AZURE_DATA_FACTORY_NAME": "Sabih-df",
        "ADF_SOURCE_PIPELINE_NAME": "pl_sales_processing_legacy",
        "FABRIC_TENANT_ID": "t", "FABRIC_CLIENT_ID": "c", "FABRIC_CLIENT_SECRET": "s",
        "FABRIC_WORKSPACE_ID": fh.WS,
        "FABRIC_TARGET_PIPELINE_ITEM_ID": item_id,
    }.items():
        os.environ[key] = value
    get_settings.cache_clear()


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


def _executions(plan_id, deployment_id, item_id, source_metrics, target_metrics, tag,
                 source_status=ExecutionStatus.SUCCEEDED, target_status=ExecutionStatus.SUCCEEDED):
    source = start_execution(
        correlation_id=f"corr-{tag}", side=ExecutionSide.SOURCE,
        pipeline_identity="pl_sales_processing_legacy", plan_id=plan_id,
    )
    source = complete_execution(
        source.execution_id, status=source_status, run_id=f"adf-run-{tag}",
        metrics=source_metrics,
    )
    target = start_execution(
        correlation_id=f"corr-{tag}", side=ExecutionSide.TARGET,
        pipeline_identity=item_id, plan_id=plan_id, deployment_id=deployment_id,
    )
    target = complete_execution(
        target.execution_id, status=target_status, run_id=f"job-{tag}",
        metrics=target_metrics,
    )
    return source, target


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


def main() -> int:
    passed: list[str] = []
    errors: list[str] = []

    print("=" * 60)
    print("  Phase 11 Verification (runtime-equivalence validation, mocked)")
    print("=" * 60)

    with TempDatabase(prefix="verify_phase11_rv_") as ctx:
        # Uses the synthetic single-Data-Pipeline plan (tests.execution_helpers)
        # rather than the full ADF fixture set: the fixture's Mapping Data
        # Flow has no real MDF -> Power Query converter anywhere in this
        # codebase, so its Dataflow Gen2 artifact is correctly NON_DEPLOYABLE
        # and the dependent Data Pipeline is correctly SKIPPED (see
        # verify_phase10_deployment.py) — it never gets a real resource id.
        from tests import execution_helpers as eh

        plan_id, approval_id, real, item_id, _transport = eh.build_real_pipeline_deployment(
            ctx.generated_dir
        )
        _enable(item_id)

        structural_before = get_latest_structural_validation()
        before_bytes = _structural_row_bytes(structural_before.deployment_id)

        # ── Exact match -> PASS ───────────────────────────────────
        s1, t1 = _executions(plan_id, real.deployment_id, item_id, _metrics(), _metrics(), "exact")
        result = RuntimeValidationService().validate(s1.execution_id, t1.execution_id)
        if result.status == RuntimeValidationStatus.PASS:
            passed.append("Exact metric match -> PASS")
        else:
            errors.append(f"expected PASS for exact match, got {result.status}")

        # ── Row-count mismatch -> FAIL ────────────────────────────
        s2, t2 = _executions(
            plan_id, real.deployment_id, item_id,
            _metrics(total_row_count=20), _metrics(total_row_count=15), "rowmismatch",
        )
        result = RuntimeValidationService().validate(s2.execution_id, t2.execution_id)
        if result.status == RuntimeValidationStatus.FAIL:
            passed.append("Row-count mismatch -> FAIL")
        else:
            errors.append(f"expected FAIL for row-count mismatch, got {result.status}")

        # ── Schema mismatch -> FAIL ───────────────────────────────
        s3, t3 = _executions(
            plan_id, real.deployment_id, item_id,
            _metrics(schemas={"order_id": "string"}), _metrics(schemas={"order_id": "int"}),
            "schemamismatch",
        )
        result = RuntimeValidationService().validate(s3.execution_id, t3.execution_id)
        if result.status == RuntimeValidationStatus.FAIL:
            passed.append("Schema mismatch -> FAIL")
        else:
            errors.append(f"expected FAIL for schema mismatch, got {result.status}")

        # ── Numeric total mismatch -> FAIL ────────────────────────
        s4, t4 = _executions(
            plan_id, real.deployment_id, item_id,
            _metrics(numeric_totals={"gross_amount": 5000.0}),
            _metrics(numeric_totals={"gross_amount": 1.0}), "numericmismatch",
        )
        result = RuntimeValidationService().validate(s4.execution_id, t4.execution_id)
        if result.status == RuntimeValidationStatus.FAIL:
            passed.append("Numeric total mismatch -> FAIL")
        else:
            errors.append(f"expected FAIL for numeric total mismatch, got {result.status}")

        # ── Grouped total mismatch -> FAIL ────────────────────────
        s5, t5 = _executions(
            plan_id, real.deployment_id, item_id,
            _metrics(grouped_totals={"region": {"North": 2000.0}}),
            _metrics(grouped_totals={"region": {"North": 1.0}}), "groupedmismatch",
        )
        result = RuntimeValidationService().validate(s5.execution_id, t5.execution_id)
        if result.status == RuntimeValidationStatus.FAIL:
            passed.append("Grouped total mismatch -> FAIL")
        else:
            errors.append(f"expected FAIL for grouped total mismatch, got {result.status}")

        # ── Within tolerance -> PASS ───────────────────────────────
        rules = RuntimeValidationRuleConfig(row_count_tolerance=2)
        s6, t6 = _executions(
            plan_id, real.deployment_id, item_id,
            _metrics(total_row_count=20), _metrics(total_row_count=21), "tolerance",
        )
        result = RuntimeValidationService().validate(s6.execution_id, t6.execution_id, rules=rules)
        if result.status == RuntimeValidationStatus.PASS:
            passed.append("Within-tolerance difference -> PASS")
        else:
            errors.append(f"expected PASS within tolerance, got {result.status}")

        # ── Duration-only deviation -> PASS_WITH_WARNINGS ─────────
        s7, t7 = _executions(
            plan_id, real.deployment_id, item_id,
            _metrics(duration_seconds=100.0), _metrics(duration_seconds=300.0), "duration",
        )
        result = RuntimeValidationService().validate(s7.execution_id, t7.execution_id)
        if result.status == RuntimeValidationStatus.PASS_WITH_WARNINGS:
            passed.append("Duration-only deviation -> PASS_WITH_WARNINGS")
        else:
            errors.append(f"expected PASS_WITH_WARNINGS for duration deviation, got {result.status}")

        # ── Missing metric -> INCONCLUSIVE (never silent PASS) ────
        s8, t8 = _executions(
            plan_id, real.deployment_id, item_id,
            _metrics(numeric_totals={}), _metrics(), "missing",
        )
        result = RuntimeValidationService().validate(s8.execution_id, t8.execution_id)
        numeric_checks = [c for c in result.checks if c.name.startswith("numeric_total")]
        if numeric_checks and all(c.status == RuntimeValidationStatus.INCONCLUSIVE for c in numeric_checks):
            passed.append("Missing metric -> INCONCLUSIVE (never a silent PASS)")
        else:
            errors.append("missing metric did not produce an INCONCLUSIVE check")

        # ── Structural validation record byte-identical proof ────
        after_bytes = _structural_row_bytes(structural_before.deployment_id)
        if after_bytes == before_bytes:
            passed.append(
                "Structural validation row is byte-identical before/after "
                "multiple runtime validation runs (including a runtime FAIL)"
            )
        else:
            errors.append("structural validation row CHANGED after runtime validation runs")

        structural_reloaded = get_structural_validation(structural_before.validation_id)
        if structural_reloaded.status == structural_before.status:
            passed.append(
                f"Structural PASS ({structural_reloaded.status.value}) and runtime FAIL "
                "coexist without contradiction"
            )
        else:
            errors.append("structural validation status changed unexpectedly")

        # ── Reporting appendix ─────────────────────────────────────
        saved = save_runtime_execution_validation(result)
        report = generate_report(structural_before.validation_id)
        if report.runtime_execution_validation is not None:
            passed.append("Report includes optional runtime-equivalence appendix (JSON)")
        else:
            errors.append("report JSON is missing the runtime_execution_validation appendix")

        html = report_path(structural_before.validation_id, "html").read_text(encoding="utf-8")
        if "runtime-equivalence validation" in html.lower() and "never alters" in html.lower():
            passed.append("Report HTML renders the runtime appendix with the separation notice")
        else:
            errors.append("report HTML is missing the runtime appendix / separation notice")

        # No raw-row content anywhere in the persisted result.
        dumped = saved.model_dump_json().lower()
        if any(tok in dumped for tok in ('"data"', '"rows"', '"row_data"')):
            errors.append("persisted runtime validation record contains a raw-row-shaped field")
        else:
            passed.append("Persisted runtime validation record has no raw-row content")

    print()
    for item in passed:
        print(f"  [PASS] {item}")
    for item in errors:
        print(f"  [FAIL] {item}")
    print()
    if errors:
        print(f"RESULT: FAIL — {len(errors)} check(s) failed.")
        return 1
    print(f"RESULT: PASS — {len(passed)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
