"""Fakes for the controlled ADF pipeline-execution SDK surface — Phase 11
tests. Never contacts Azure."""

from typing import Optional

PIPELINE_NAME = "pl_sales_processing_legacy"


class _FakePipelinesOp:
    def __init__(self, run_id: str = "run-1", raise_exc: Optional[Exception] = None):
        self.calls: list[tuple] = []
        self.run_id = run_id
        self.raise_exc = raise_exc

    def create_run(self, resource_group, factory_name, pipeline_name):
        self.calls.append((resource_group, factory_name, pipeline_name))
        if self.raise_exc:
            raise self.raise_exc
        return type("RunResponse", (), {"run_id": self.run_id})()


class _FakePipelineRunsOp:
    def __init__(self, statuses: list[str]):
        self.statuses = list(statuses)
        self.get_calls: list[str] = []
        self.cancel_calls: list[str] = []

    def get(self, resource_group, factory_name, run_id):
        self.get_calls.append(run_id)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return type("Run", (), {"status": status})()

    def cancel(self, resource_group, factory_name, run_id):
        self.cancel_calls.append(run_id)


class FakeDataFactoryExecutionClient:
    """Fake azure-mgmt-datafactory client exposing only the surface the
    controlled executor uses: pipelines.create_run + pipeline_runs.get/cancel."""

    def __init__(
        self,
        statuses: Optional[list[str]] = None,
        run_id: str = "run-1",
        create_run_exc: Optional[Exception] = None,
    ):
        self.pipelines = _FakePipelinesOp(run_id=run_id, raise_exc=create_run_exc)
        self.pipeline_runs = _FakePipelineRunsOp(statuses or ["Succeeded"])


def make_executor(
    statuses: Optional[list[str]] = None,
    run_id: str = "run-1",
    create_run_exc: Optional[Exception] = None,
    pipeline_name: str = PIPELINE_NAME,
    **overrides,
):
    from src.connectors.azure_adf_executor import AzureADFExecutor

    client = FakeDataFactoryExecutionClient(
        statuses=statuses, run_id=run_id, create_run_exc=create_run_exc
    )
    params = dict(
        tenant_id="tenant",
        client_id="client",
        client_secret="ADF-EXEC-SECRET-should-never-leak",
        subscription_id="11111111-1111-1111-1111-111111111111",
        resource_group="AzureFabricMigrationPOC",
        data_factory_name="Sabih-df",
        pipeline_name=pipeline_name,
        timeout_seconds=overrides.pop("timeout_seconds", 300),
        poll_interval_seconds=overrides.pop("poll_interval_seconds", 0),
        credential_factory=lambda *a, **k: object(),
        datafactory_client_factory=lambda *a, **k: client,
        sleep_fn=lambda _s: None,
    )
    params.update(overrides)
    return AzureADFExecutor(**params), client


# ── Full plan/approval/REAL-deployment/structural-validation fixture ──
#
# Builds everything a Phase 11 target execution / runtime validation needs
# to pass its pre-flight authorization gate: an approved, unchanged package
# containing a single deployable Data Pipeline artifact, deployed REAL
# against a fake Fabric transport, plus a PASSED structural validation row
# for that deployment (inserted directly — Phase 8's StructuralValidationService
# only supports MOCK deployments, so a Phase 11 REAL-deployment record is
# stubbed in with the same store used everywhere else).

PIPELINE_ITEM_NAME = "pl_target_pipeline"


def _synthetic_pipeline_plan():
    from src.models.schemas import (
        DeployableTargetType,
        GeneratedArtifact,
        MigrationAction,
        MigrationActionType,
        MigrationPlan,
        MigrationRisk,
        TargetItemType,
    )
    from src.artifacts import build_package

    pipeline_artifact = GeneratedArtifact(
        artifact_id="pipeline:target",
        source_reference="pipeline:legacy",
        target_type=DeployableTargetType.DATA_PIPELINE,
        target_name=PIPELINE_ITEM_NAME,
        generated_definition={
            "type": "FabricDataPipeline",
            "name": PIPELINE_ITEM_NAME,
            "properties": {"activities": [], "parameters": {}, "variables": {}},
        },
        content_digest="",
    )
    return MigrationPlan(
        executable=True,
        overall_risk=MigrationRisk.LOW,
        actions=[
            MigrationAction(
                order=1,
                action_type=MigrationActionType.VERIFY_WORKSPACE,
                target_item_type=TargetItemType.WORKSPACE,
                target_item_name="ws",
                reason="verify",
            )
        ],
        generated_package=build_package([pipeline_artifact]),
    )


def ensure_discovery_snapshot() -> int:
    """Save a minimal DiscoveryResult if none exists yet; return its id.

    Reports require a persisted discovery snapshot to render — the
    synthetic Phase 11 plan below has no real discovery run behind it, so
    this stubs one in via the same store every other phase uses.
    """
    from src.migration.discovery_store import get_latest_discovery, save_discovery
    from src.models.schemas import DiscoveryResult

    existing = get_latest_discovery()
    if existing is not None:
        return existing["id"]
    return save_discovery(DiscoveryResult())["id"]


def build_plan_and_approval(gen_dir):
    """Build + write + save + approve the synthetic single-pipeline plan.

    Returns (plan_id, approval_id).
    """
    from pathlib import Path

    from src.approvals import approval_service as appr
    from src.artifacts import write_package
    from src.migration.plan_store import save_plan

    ensure_discovery_snapshot()
    plan = _synthetic_pipeline_plan()
    write_package(plan.generated_package, Path(gen_dir))
    rec = save_plan(plan, assessment_id=1)
    plan_id = rec["id"]
    approval = appr.request_approval(plan_id, "alice")
    appr.approve(approval.approval_id, "bob")
    return plan_id, approval.approval_id


def build_real_pipeline_deployment(gen_dir, transport=None):
    """Return (plan_id, approval_id, deployment_result, item_id, transport)."""
    from src.migration.deployment import DeploymentService
    from src.models.schemas import DeployableTargetType, DeploymentMode
    from src.migration.plan_store import compute_plan_package_fingerprint, get_plan
    from src.validation.structural_store import save_structural_validation
    from src.models.schemas import (
        StructuralValidationResult,
        StructuralValidationSummary,
        ValidationStatus,
    )
    from tests import fabric_helpers as fh

    plan_id, approval_id = build_plan_and_approval(gen_dir)

    transport = transport or fh.FakeFabricTransport()
    svc = DeploymentService(fabric_client=fh.make_client(transport=transport))
    deployment = svc.deploy(plan_id, approval_id, DeploymentMode.REAL)

    pipeline_step = next(
        s for s in deployment.steps
        if s.target_item_type == DeployableTargetType.DATA_PIPELINE.value
    )
    item_id = pipeline_step.resource_id

    rec = get_plan(plan_id)
    fingerprint = compute_plan_package_fingerprint(rec["plan"])
    structural = StructuralValidationResult(
        discovery_id=ensure_discovery_snapshot(),
        deployment_id=deployment.deployment_id,
        plan_id=plan_id,
        approval_id=approval_id,
        package_fingerprint=fingerprint,
        status=ValidationStatus.PASSED,
        summary=StructuralValidationSummary(total_checks=1, passed=1),
        checks=[],
    )
    save_structural_validation(structural)

    return plan_id, approval_id, deployment, item_id, transport
