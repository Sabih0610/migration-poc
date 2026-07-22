"""Phase 11 controlled execution verification (fully mocked, no network).

Exercises: disabled-by-default, source (ADF) pipeline-name boundary
rejection, target (Fabric) item-id boundary rejection, a successful
source execution, a successful target execution (after full
authorization), timeout + best-effort cancellation on both sides,
restart persistence, and duplicate-execution prevention.

Zero real Azure/Fabric calls are ever made — every SDK/HTTP surface is
injected with an in-memory fake.

Exit 0 = PASS, non-zero = FAIL.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database as db_module
from scripts.verify_helper import TempDatabase
from src.approvals import approval_service as appr
from src.artifacts import write_package
from src.config import get_settings
from src.connectors.adf_source import FixtureADFSource
from src.connectors.azure_adf_executor import AzureExecutionError
from src.connectors.fabric_pipeline_executor import FabricPipelineExecutor
from src.database import Base
from src.execution.execution_service import (
    ExecutionAuthorizationError,
    SourceExecutionService,
    TargetExecutionService,
    source_readiness,
    target_readiness,
)
from src.execution.execution_store import (
    DuplicateExecutionError,
    get_execution,
    start_execution,
)
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
)
from src.validation.structural_store import save_structural_validation
from src.validation.structural_validator import StructuralValidationService
from tests import execution_helpers as eh
from tests import fabric_helpers as fh

FIXTURES = PROJECT_ROOT / "fixtures"


def _enable(monkeypatch_env, item_id):
    for key, value in {
        "RUNTIME_EXECUTION_ENABLED": "true",
        "AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "c", "AZURE_CLIENT_SECRET": "s",
        "AZURE_SUBSCRIPTION_ID": "11111111-1111-1111-1111-111111111111",
        "AZURE_RESOURCE_GROUP": "AzureFabricMigrationPOC",
        "AZURE_DATA_FACTORY_NAME": "Sabih-df",
        "ADF_SOURCE_PIPELINE_NAME": eh.PIPELINE_NAME,
        "FABRIC_TENANT_ID": "t", "FABRIC_CLIENT_ID": "c", "FABRIC_CLIENT_SECRET": "s",
        "FABRIC_WORKSPACE_ID": fh.WS,
        "FABRIC_TARGET_PIPELINE_ITEM_ID": item_id,
    }.items():
        monkeypatch_env[key] = value
    import os
    os.environ.update(monkeypatch_env)
    get_settings.cache_clear()


def main() -> int:
    passed: list[str] = []
    errors: list[str] = []

    print("=" * 60)
    print("  Phase 11 Verification (controlled execution, fully mocked)")
    print("=" * 60)

    with TempDatabase(prefix="verify_phase11_exec_") as ctx:
        # ── 1. Disabled by default ───────────────────────────────
        settings = get_settings()
        if settings.runtime_execution_enabled or settings.runtime_execution_ready():
            errors.append("runtime execution is NOT disabled by default")
        else:
            passed.append("Runtime execution disabled by default")

        try:
            SourceExecutionService().start()
            errors.append("source execution ran while disabled")
        except AzureExecutionError as exc:
            passed.append(f"Source execution correctly refused while disabled ({exc.code})")

        print(source_readiness())
        print(target_readiness())

        # ── 2. Source (ADF) boundary rejection ───────────────────
        executor, adf_client = eh.make_executor(statuses=["Succeeded"])
        try:
            executor.start_run("not_the_configured_pipeline")
            errors.append("source executor accepted a free-form pipeline name")
        except AzureExecutionError as exc:
            if exc.code == "ADF_EXECUTION_BOUNDARY_VIOLATION" and not adf_client.pipelines.calls:
                passed.append("Source pipeline-name boundary enforced (no call issued)")
            else:
                errors.append(f"unexpected boundary behavior: {exc.code}")

        # ── 3. Successful source execution ───────────────────────
        _enable({}, "unused-for-source-only")
        ok_executor, _ = eh.make_executor(statuses=["InProgress", "Succeeded"])
        source_result = SourceExecutionService(executor=ok_executor).start()
        if source_result.status == ExecutionStatus.SUCCEEDED and source_result.metrics:
            passed.append(
                f"Source execution succeeded (execution_id={source_result.execution_id}, "
                f"run_id={source_result.run_id})"
            )
        else:
            errors.append("source execution did not report SUCCEEDED with metrics")

        # ── 4. Source timeout + best-effort cancel ───────────────
        timeout_executor, timeout_client = eh.make_executor(
            statuses=["InProgress", "InProgress"], timeout_seconds=0
        )
        timeout_result = SourceExecutionService(executor=timeout_executor).start()
        if (
            timeout_result.status == ExecutionStatus.TIMED_OUT
            and timeout_client.pipeline_runs.cancel_calls
        ):
            passed.append("Source timeout triggers best-effort cancellation")
        else:
            errors.append("source timeout/cancel handling did not behave as expected")

        # ── 5. Build an approved REAL deployment + structural PASS ──
        # Uses the synthetic single-Data-Pipeline plan (tests.execution_helpers)
        # rather than the full ADF fixture set: the fixture's Mapping Data
        # Flow has no real MDF -> Power Query converter anywhere in this
        # codebase, so its Dataflow Gen2 artifact is correctly NON_DEPLOYABLE
        # and the dependent Data Pipeline is correctly SKIPPED (see
        # verify_phase10_deployment.py) — it never gets a real resource id.
        transport = fh.FakeFabricTransport()
        plan_id, approval_id, real, item_id, transport = eh.build_real_pipeline_deployment(
            ctx.generated_dir, transport
        )

        # Wrong item id rejected.
        try:
            _enable({}, "a-different-item-id")
            TargetExecutionService().start(plan_id=plan_id, deployment_id=real.deployment_id)
            errors.append("target execution accepted a mismatched item id")
        except ExecutionAuthorizationError as exc:
            if exc.code == "PIPELINE_ITEM_NOT_DEPLOYED":
                passed.append("Target item-id boundary enforced")
            else:
                errors.append(f"unexpected target boundary error: {exc.code}")
        _enable({}, item_id)

        # ── 6. Successful target execution ───────────────────────
        transport.job_status_sequence = ["NotStarted", "Completed"]
        fabric_executor = FabricPipelineExecutor(
            fh.make_client(transport=transport), item_id=item_id,
            poll_interval_seconds=0, sleep_fn=lambda _s: None,
        )
        target_result = TargetExecutionService(executor=fabric_executor).start(
            plan_id=plan_id, deployment_id=real.deployment_id
        )
        if target_result.status == ExecutionStatus.SUCCEEDED and target_result.metrics:
            passed.append(
                f"Target execution succeeded (execution_id={target_result.execution_id}, "
                f"run_id={target_result.run_id})"
            )
        else:
            errors.append("target execution did not report SUCCEEDED with metrics")

        # ── 7. Target timeout + best-effort cancel ───────────────
        timeout_transport = fh.FakeFabricTransport()
        timeout_transport.job_status_sequence = ["NotStarted", "NotStarted"]
        timeout_fabric_executor = FabricPipelineExecutor(
            fh.make_client(transport=timeout_transport), item_id=item_id,
            timeout_seconds=0, poll_interval_seconds=0, sleep_fn=lambda _s: None,
        )
        # Use a fresh execution slot (previous target execution already completed).
        timeout_target = TargetExecutionService(executor=timeout_fabric_executor).start(
            plan_id=plan_id, deployment_id=real.deployment_id
        )
        if timeout_target.status == ExecutionStatus.TIMED_OUT and timeout_transport.job_cancel_calls:
            passed.append("Target timeout triggers best-effort cancellation")
        else:
            errors.append("target timeout/cancel handling did not behave as expected")

        # ── 8. Duplicate-execution prevention ────────────────────
        start_execution(
            correlation_id="dup-check", side=ExecutionSide.SOURCE,
            pipeline_identity=eh.PIPELINE_NAME,
        )
        try:
            SourceExecutionService(executor=ok_executor).start()
            errors.append("duplicate source execution was not rejected")
        except DuplicateExecutionError:
            passed.append("Duplicate source execution correctly rejected")

        # ── 9. No delete calls anywhere ───────────────────────────
        all_methods = {c[0] for c in transport.calls} | {c[0] for c in timeout_transport.calls}
        if "DELETE" in all_methods:
            errors.append("a DELETE call was issued somewhere in the execution path")
        else:
            passed.append("No DELETE calls issued anywhere in execution path")

        # ── 10. Restart persistence ───────────────────────────────
        db_path = Path(ctx.tmp_dir) / "restart_check.db"
        url = f"sqlite:///{db_path.as_posix()}"
        engine1 = create_engine(url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine1)
        old_engine, old_session = db_module._engine, db_module._SessionLocal
        db_module._engine = engine1
        db_module._SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine1)
        written = start_execution(
            correlation_id="restart", side=ExecutionSide.SOURCE, pipeline_identity="pl_x"
        )
        engine1.dispose()

        engine2 = create_engine(url, connect_args={"check_same_thread": False})
        db_module._engine = engine2
        db_module._SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine2)
        reloaded = get_execution(written.execution_id)
        engine2.dispose()
        db_module._engine, db_module._SessionLocal = old_engine, old_session

        if reloaded is not None and reloaded.correlation_id == "restart":
            passed.append("Execution record survives simulated process restart")
        else:
            errors.append("execution record did not survive simulated restart")

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
