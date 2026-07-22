"""Phase 11 controlled source/target pipeline execution tests.

All Azure and Fabric access is mocked/injected — zero real cloud calls.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database as db_module
from src.config import get_settings
from src.connectors.azure_adf_executor import AzureExecutionError
from src.connectors.fabric_client import FabricError
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
    complete_execution,
    get_execution,
    get_running_execution,
    list_executions,
    start_execution,
)
from src.migration.deployment import DeploymentService
from src.models.schemas import (
    DeployableTargetType,
    DeploymentMode,
    ExecutionSide,
    ExecutionStatus,
    RuntimeMetrics,
)
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
    monkeypatch.setenv("GENERATED_ARTIFACTS_DIR", str(gen))
    get_settings.cache_clear()
    yield gen
    get_settings.cache_clear()


def _enable_runtime_execution(monkeypatch, pipeline_item_id: str, pipeline_name: str = eh.PIPELINE_NAME):
    for key, value in {
        "RUNTIME_EXECUTION_ENABLED": "true",
        "AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "c", "AZURE_CLIENT_SECRET": "s",
        "AZURE_SUBSCRIPTION_ID": "11111111-1111-1111-1111-111111111111",
        "AZURE_RESOURCE_GROUP": "AzureFabricMigrationPOC",
        "AZURE_DATA_FACTORY_NAME": "Sabih-df",
        "ADF_SOURCE_PIPELINE_NAME": pipeline_name,
        "FABRIC_TENANT_ID": "t", "FABRIC_CLIENT_ID": "c", "FABRIC_CLIENT_SECRET": "s",
        "FABRIC_WORKSPACE_ID": fh.WS,
        "FABRIC_TARGET_PIPELINE_ITEM_ID": pipeline_item_id,
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _authorized_target_env(env, monkeypatch, transport=None):
    """Full approved + REAL-deployed + structurally-PASSED + enabled setup."""
    plan_id, approval_id, real, item_id, transport = eh.build_real_pipeline_deployment(
        env, transport
    )
    _enable_runtime_execution(monkeypatch, item_id)
    return plan_id, real.deployment_id, item_id, transport


# ── Configuration / disabled-by-default ─────────────────────────────────


def test_runtime_execution_disabled_by_default():
    settings = get_settings()
    assert settings.runtime_execution_enabled is False
    assert settings.runtime_execution_ready() is False


def test_source_target_readiness_report_disabled_by_default():
    source = source_readiness()
    target = target_readiness()
    assert source["ready"] is False
    assert target["ready"] is False


# ── AzureADFExecutor: boundary, success, failure, timeout, throttling ───


def test_adf_executor_rejects_mismatched_pipeline_name():
    executor, client = eh.make_executor(statuses=["Succeeded"])
    with pytest.raises(AzureExecutionError) as exc:
        executor.start_run("some_other_pipeline")
    assert exc.value.code == "ADF_EXECUTION_BOUNDARY_VIOLATION"
    assert not client.pipelines.calls


def test_adf_executor_always_uses_configured_factory_and_rg():
    executor, client = eh.make_executor(statuses=["Succeeded"])
    executor.start_run(eh.PIPELINE_NAME)
    assert client.pipelines.calls == [
        ("AzureFabricMigrationPOC", "Sabih-df", eh.PIPELINE_NAME)
    ]


def test_adf_executor_run_success():
    executor, _ = eh.make_executor(statuses=["InProgress", "Succeeded"])
    result = executor.run_to_terminal(eh.PIPELINE_NAME)
    assert result.status == "Succeeded"
    assert result.run_id == "run-1"


def test_adf_executor_run_failure():
    executor, _ = eh.make_executor(statuses=["Failed"])
    result = executor.run_to_terminal(eh.PIPELINE_NAME)
    assert result.status == "Failed"


def test_adf_executor_timeout_cancels_run():
    executor, client = eh.make_executor(statuses=["InProgress"], timeout_seconds=0)
    result = executor.run_to_terminal(eh.PIPELINE_NAME)
    assert result.status == "TimedOut"
    assert result.safe_error_category == "ADF_EXECUTION_TIMEOUT"
    assert client.pipeline_runs.cancel_calls == ["run-1"]


def test_adf_executor_throttled_error_sanitized():
    class _Throttled(Exception):
        status_code = 429

    executor, _ = eh.make_executor(create_run_exc=_Throttled("rate limited, secret=abc"))
    with pytest.raises(AzureExecutionError) as exc:
        executor.start_run(eh.PIPELINE_NAME)
    assert exc.value.code == "ADF_EXECUTION_THROTTLED"
    assert "secret" not in str(exc.value)


def test_adf_executor_never_calls_definition_or_trigger_methods():
    executor, client = eh.make_executor(statuses=["Succeeded"])
    executor.run_to_terminal(eh.PIPELINE_NAME)
    for forbidden in ("pipelines_update", "triggers", "publish", "delete"):
        assert not hasattr(client, forbidden)
    assert not hasattr(executor, "update_pipeline")
    assert not hasattr(executor, "delete_pipeline")
    assert not hasattr(executor, "publish")


# ── FabricPipelineExecutor: boundary, success, failure, timeout ─────────


def _fabric_executor(transport=None, item_id="item-101", **overrides):
    transport = transport or fh.FakeFabricTransport()
    client = fh.make_client(transport=transport)
    return FabricPipelineExecutor(
        client, item_id=item_id, poll_interval_seconds=0, sleep_fn=lambda _s: None,
        **overrides,
    ), transport


def test_fabric_executor_rejects_mismatched_item_id():
    executor, transport = _fabric_executor()
    with pytest.raises(FabricError) as exc:
        executor.start_run("some-other-item")
    assert exc.value.code == "FABRIC_BOUNDARY_VIOLATION"
    assert not transport.calls


def test_fabric_executor_run_success():
    transport = fh.FakeFabricTransport()
    transport.job_status_sequence = ["NotStarted", "Completed"]
    executor, _ = _fabric_executor(transport=transport)
    result = executor.run_to_terminal("item-101")
    assert result.status == "Completed"


def test_fabric_executor_run_failure():
    transport = fh.FakeFabricTransport()
    transport.job_status_sequence = ["Failed"]
    executor, _ = _fabric_executor(transport=transport)
    result = executor.run_to_terminal("item-101")
    assert result.status == "Failed"


def test_fabric_executor_timeout_cancels_run():
    transport = fh.FakeFabricTransport()
    transport.job_status_sequence = ["NotStarted"]
    executor, transport = _fabric_executor(transport=transport, timeout_seconds=0)
    result = executor.run_to_terminal("item-101")
    assert result.status == "TimedOut"
    assert transport.job_cancel_calls


def test_fabric_executor_throttled_then_succeeds():
    transport = fh.FakeFabricTransport()
    transport.throttle_times = 2
    executor, _ = _fabric_executor(transport=transport)
    result = executor.run_to_terminal("item-101")
    assert result.status == "Completed"


def test_fabric_executor_never_issues_delete_calls():
    transport = fh.FakeFabricTransport()
    transport.job_status_sequence = ["Completed"]
    executor, transport = _fabric_executor(transport=transport)
    executor.run_to_terminal("item-101")
    assert not any(call[0] == "DELETE" for call in transport.calls)


# ── Execution store: persistence, dedup, restart ────────────────────────


def test_execution_store_no_raw_row_field(env):
    result = start_execution(
        correlation_id="corr-1", side=ExecutionSide.SOURCE, pipeline_identity="pl_x"
    )
    complete_execution(
        result.execution_id, status=ExecutionStatus.SUCCEEDED, run_id="run-1",
        metrics=RuntimeMetrics(total_row_count=5),
    )
    dumped = get_execution(result.execution_id).model_dump()
    assert "data" not in dumped
    assert "raw_rows" not in dumped
    assert "row_data" not in dumped


def test_execution_store_duplicate_prevention(env):
    start_execution(correlation_id="c1", side=ExecutionSide.SOURCE, pipeline_identity="pl_x")
    with pytest.raises(Exception):
        start_execution(correlation_id="c2", side=ExecutionSide.SOURCE, pipeline_identity="pl_x")


def test_execution_store_dedup_allows_after_completion(env):
    first = start_execution(correlation_id="c1", side=ExecutionSide.SOURCE, pipeline_identity="pl_x")
    complete_execution(first.execution_id, status=ExecutionStatus.SUCCEEDED)
    second = start_execution(correlation_id="c2", side=ExecutionSide.SOURCE, pipeline_identity="pl_x")
    assert second.execution_id != first.execution_id


def test_execution_store_restart_persistence(tmp_path):
    """A record written by one engine/session-factory instance is readable
    by a completely fresh one against the same db file (proves it survives
    process restart, not just in-memory)."""
    db_path = tmp_path / "restart.db"
    url = f"sqlite:///{db_path.as_posix()}"

    engine1 = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine1)
    db_module._engine = engine1
    db_module._SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine1)
    written = start_execution(
        correlation_id="restart-corr", side=ExecutionSide.TARGET, pipeline_identity="item-999"
    )
    complete_execution(written.execution_id, status=ExecutionStatus.SUCCEEDED, run_id="job-1")
    engine1.dispose()

    # Fresh engine + session factory against the exact same db file path.
    engine2 = create_engine(url, connect_args={"check_same_thread": False})
    db_module._engine = engine2
    db_module._SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine2)
    reloaded = get_execution(written.execution_id)
    engine2.dispose()

    assert reloaded is not None
    assert reloaded.correlation_id == "restart-corr"
    assert reloaded.run_id == "job-1"
    assert reloaded.status == ExecutionStatus.SUCCEEDED


def test_execution_history_filterable(env):
    a = start_execution(correlation_id="c1", side=ExecutionSide.SOURCE, pipeline_identity="pl_x", plan_id=1)
    complete_execution(a.execution_id, status=ExecutionStatus.SUCCEEDED)
    b = start_execution(correlation_id="c2", side=ExecutionSide.TARGET, pipeline_identity="item-1", plan_id=1)
    complete_execution(b.execution_id, status=ExecutionStatus.FAILED)

    all_results = list_executions()
    assert len(all_results) == 2
    source_only = list_executions(side=ExecutionSide.SOURCE)
    assert len(source_only) == 1 and source_only[0].execution_id == a.execution_id
    failed_only = list_executions(status=ExecutionStatus.FAILED)
    assert len(failed_only) == 1 and failed_only[0].execution_id == b.execution_id


# ── SourceExecutionService ───────────────────────────────────────────────


def test_source_execution_service_disabled_raises(env):
    with pytest.raises(AzureExecutionError):
        SourceExecutionService().start()


def test_source_execution_service_success(env, monkeypatch):
    _enable_runtime_execution(monkeypatch, pipeline_item_id="unused-for-source")
    executor, _ = eh.make_executor(statuses=["InProgress", "Succeeded"])
    result = SourceExecutionService(executor=executor).start()
    assert result.status == ExecutionStatus.SUCCEEDED
    assert result.metrics is not None


def test_source_execution_service_failure(env, monkeypatch):
    _enable_runtime_execution(monkeypatch, pipeline_item_id="unused-for-source")
    executor, _ = eh.make_executor(statuses=["Failed"])
    result = SourceExecutionService(executor=executor).start()
    assert result.status == ExecutionStatus.FAILED
    assert result.metrics is None


def test_source_execution_service_duplicate_prevention(env, monkeypatch):
    _enable_runtime_execution(monkeypatch, pipeline_item_id="unused-for-source")
    executor, _ = eh.make_executor(statuses=["InProgress", "InProgress", "Succeeded"], timeout_seconds=1_000_000)

    running = get_running_execution(ExecutionSide.SOURCE, eh.PIPELINE_NAME)
    assert running is None
    start_execution(correlation_id="dup", side=ExecutionSide.SOURCE, pipeline_identity=eh.PIPELINE_NAME)
    with pytest.raises(DuplicateExecutionError):
        SourceExecutionService(executor=executor).start()


# ── TargetExecutionService authorization gate ────────────────────────────


def test_target_execution_disabled_raises(env):
    with pytest.raises(ExecutionAuthorizationError) as exc:
        TargetExecutionService().start(plan_id=1, deployment_id=1)
    assert exc.value.code == "RUNTIME_EXECUTION_DISABLED"


def test_target_execution_rejects_unknown_deployment(env, monkeypatch):
    _enable_runtime_execution(monkeypatch, pipeline_item_id="item-x")
    with pytest.raises(ExecutionAuthorizationError) as exc:
        TargetExecutionService().start(plan_id=1, deployment_id=999999)
    assert exc.value.code == "DEPLOYMENT_NOT_FOUND"


def test_target_execution_rejects_mock_deployment(env, monkeypatch):
    plan_id, approval_id = eh.build_plan_and_approval(env)
    mock_result = DeploymentService().deploy(plan_id, approval_id, DeploymentMode.MOCK)
    _enable_runtime_execution(monkeypatch, pipeline_item_id="item-x")
    with pytest.raises(ExecutionAuthorizationError) as exc:
        TargetExecutionService().start(plan_id=plan_id, deployment_id=mock_result.deployment_id)
    assert exc.value.code == "DEPLOYMENT_NOT_REAL"


def test_target_execution_rejects_wrong_item_id(env, monkeypatch):
    plan_id, deployment_id, item_id, _ = _authorized_target_env(env, monkeypatch)
    # Reconfigure the boundary to a *different* item id after the fact.
    _enable_runtime_execution(monkeypatch, pipeline_item_id="a-completely-different-item")
    with pytest.raises(ExecutionAuthorizationError) as exc:
        TargetExecutionService().start(plan_id=plan_id, deployment_id=deployment_id)
    assert exc.value.code == "PIPELINE_ITEM_NOT_DEPLOYED"


def test_target_execution_rejects_when_structural_validation_missing(env, monkeypatch):
    plan_id, approval_id = eh.build_plan_and_approval(env)
    transport = fh.FakeFabricTransport()
    real = DeploymentService(fabric_client=fh.make_client(transport=transport)).deploy(
        plan_id, approval_id, DeploymentMode.REAL
    )
    pipeline_step = next(
        s for s in real.steps
        if s.target_item_type == DeployableTargetType.DATA_PIPELINE.value and s.resource_id
    )
    _enable_runtime_execution(monkeypatch, pipeline_item_id=pipeline_step.resource_id)
    with pytest.raises(ExecutionAuthorizationError) as exc:
        TargetExecutionService().start(plan_id=plan_id, deployment_id=real.deployment_id)
    assert exc.value.code == "STRUCTURAL_VALIDATION_NOT_PASSED"


def test_target_execution_success(env, monkeypatch):
    plan_id, deployment_id, item_id, transport = _authorized_target_env(env, monkeypatch)
    transport.job_status_sequence = ["Completed"]
    executor = FabricPipelineExecutor(
        fh.make_client(transport=transport), item_id=item_id,
        poll_interval_seconds=0, sleep_fn=lambda _s: None,
    )
    result = TargetExecutionService(executor=executor).start(
        plan_id=plan_id, deployment_id=deployment_id
    )
    assert result.status == ExecutionStatus.SUCCEEDED
    assert result.metrics is not None


def test_target_execution_failure(env, monkeypatch):
    plan_id, deployment_id, item_id, transport = _authorized_target_env(env, monkeypatch)
    transport.job_status_sequence = ["Failed"]
    executor = FabricPipelineExecutor(
        fh.make_client(transport=transport), item_id=item_id,
        poll_interval_seconds=0, sleep_fn=lambda _s: None,
    )
    result = TargetExecutionService(executor=executor).start(
        plan_id=plan_id, deployment_id=deployment_id
    )
    assert result.status == ExecutionStatus.FAILED


def test_target_execution_duplicate_prevention(env, monkeypatch):
    plan_id, deployment_id, item_id, transport = _authorized_target_env(env, monkeypatch)
    start_execution(correlation_id="dup", side=ExecutionSide.TARGET, pipeline_identity=item_id)
    executor = FabricPipelineExecutor(
        fh.make_client(transport=transport), item_id=item_id,
        poll_interval_seconds=0, sleep_fn=lambda _s: None,
    )
    with pytest.raises(DuplicateExecutionError):
        TargetExecutionService(executor=executor).start(plan_id=plan_id, deployment_id=deployment_id)


# ── Secret redaction sanity (execution path never leaks tokens) ─────────


def test_execution_result_never_contains_secrets(env, monkeypatch):
    _enable_runtime_execution(monkeypatch, pipeline_item_id="unused")
    executor, _ = eh.make_executor(statuses=["Succeeded"])
    result = SourceExecutionService(executor=executor).start()
    blob = result.model_dump_json().lower()
    assert "adf-exec-secret-should-never-leak" not in blob
    assert fh.FAKE_TOKEN.lower() not in blob
