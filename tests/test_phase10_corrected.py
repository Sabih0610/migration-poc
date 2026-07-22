"""Phase 10 correction regression tests (mocked Fabric, no network).

Covers the twelve corrected behaviors audited into the real-Fabric write
path: Connection/LakehouseTable/FabricSchedule are no longer treated as
ordinary workspace items, internal JSON is never sent to Fabric as if it
were an official public definition, read-back digest verification is
enforced, non-deployable artifacts are blocked (never faked), capacity
verifiability is reported honestly, and existing safety controls
(no-delete, no ADF writes, recursive secret redaction, MOCK/DRY_RUN) all
still hold.
"""

from pathlib import Path

import pytest

from src.artifacts import compute_artifact_digest
from src.connectors import fabric_definition_adapter as adapter
from src.connectors.fabric_client import (
    CODE_NON_DEPLOYABLE,
    FabricClient,
    FabricError,
)
from src.models.schemas import DeployableTargetType, GeneratedArtifact
from src.reports.report_service import redact_secrets
from tests import fabric_helpers as fh

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# ── 1. Connection handled outside workspace item APIs ────────────


def test_connection_not_a_workspace_item():
    package = fh.full_package()
    connection = fh.artifact_of(package, DeployableTargetType.CONNECTION)
    t = fh.FakeFabricTransport()
    client = fh.make_client(transport=t)

    outcome = client.deploy_artifact(connection)

    assert outcome.status == "created" and outcome.item_id
    paths = [url for (_method, url, _h, _b) in t.calls]
    assert any(p.endswith("/connections") for p in paths)
    assert not any(p.endswith(f"/workspaces/{fh.WS}/items") for p in paths)
    assert not any("/items" in p and "connections" not in p for p in paths if "items" in p)


# ── 2. Schedule attached to pipeline item, enabled=false by default ─


def _pipeline_and_schedule(package):
    pipeline = fh.artifact_of(package, DeployableTargetType.DATA_PIPELINE)
    schedule = fh.artifact_of(package, DeployableTargetType.SCHEDULE)
    return pipeline, schedule


def test_schedule_attached_to_pipeline_disabled_by_default():
    package = fh.full_package()
    pipeline, schedule = _pipeline_and_schedule(package)
    t = fh.FakeFabricTransport()
    client = fh.make_client(transport=t)

    pipeline_outcome = client.deploy_artifact(pipeline)
    dependency_ids = {pipeline.artifact_id: pipeline_outcome.item_id}

    schedule_outcome = client.deploy_artifact(schedule, dependency_ids)

    assert schedule_outcome.status == "created" and schedule_outcome.item_id
    sched_post_calls = [
        (m, u, b) for (m, u, _h, b) in t.calls
        if "/jobs/" in u and "/schedules" in u and m == "POST"
    ]
    assert sched_post_calls, "no job-scheduler POST call was made"
    method, url, body = sched_post_calls[0]
    assert f"/items/{pipeline_outcome.item_id}/jobs/" in url
    assert body["enabled"] is False


def test_schedule_requires_real_parent_pipeline_id():
    package = fh.full_package()
    _pipeline, schedule = _pipeline_and_schedule(package)
    client = fh.make_client()
    with pytest.raises(FabricError):
        client.deploy_artifact(schedule, {})  # no parent pipeline id known


# ── 3. LakehouseTable deferred, never fake-created ────────────────


def test_lakehouse_table_deferred_no_network_call():
    package = fh.full_package()
    table = fh.artifact_of(package, DeployableTargetType.LAKEHOUSE_TABLE)
    t = fh.FakeFabricTransport()
    client = fh.make_client(transport=t)

    outcome = client.deploy_artifact(table, {})

    assert outcome.status == "deferred"
    assert outcome.materialization_status == "DEFERRED_TO_RUNTIME"
    assert outcome.item_id is None
    assert t.calls == []  # no POST, no GET — no network call at all


# ── 4. Internal JSON rejected as an official public definition ───


def test_adapter_refuses_unconvertible_dataflow():
    package = fh.full_package()
    dataflow = fh.artifact_of(package, DeployableTargetType.DATAFLOW_GEN2)

    built = adapter.build_definition(dataflow)

    assert built.deployable is False
    assert "Power Query" in built.reason or "Power Query M" in built.reason


def test_non_deployable_dataflow_blocks_before_any_http_call():
    package = fh.full_package()
    dataflow = fh.artifact_of(package, DeployableTargetType.DATAFLOW_GEN2)
    t = fh.FakeFabricTransport()
    client = fh.make_client(transport=t)

    with pytest.raises(FabricError) as exc:
        client.deploy_artifact(dataflow)

    assert exc.value.code == CODE_NON_DEPLOYABLE
    assert t.calls == []  # never contacted Fabric with a fake definition


def test_adapter_accepts_dataflow_with_real_power_query():
    package = fh.full_package()
    deployable = fh.deployable_dataflow_artifact(package)
    built = adapter.build_definition(deployable)
    assert built.deployable is True
    assert any(p["path"] == "mashup.pq" for p in built.parts)


# ── 5 & 6. Definition read-back: match and mismatch ───────────────


def test_readback_digest_match():
    package = fh.full_package()
    pipeline = fh.artifact_of(package, DeployableTargetType.DATA_PIPELINE)
    t = fh.FakeFabricTransport()
    client = fh.make_client(transport=t)

    outcome = client.deploy_artifact(pipeline)

    assert outcome.readback_status == "MATCH"
    assert outcome.readback_digest


def test_readback_digest_mismatch_is_flagged_not_silently_passed():
    package = fh.full_package()
    pipeline = fh.artifact_of(package, DeployableTargetType.DATA_PIPELINE)
    t = fh.FakeFabricTransport()
    # Fabric reports back a different definition than what we sent.
    t.readback_override = {"parts": [{"path": "pipeline-content.json", "payload": "dGFtcGVyZWQ="}]}
    client = fh.make_client(transport=t)

    outcome = client.deploy_artifact(pipeline)

    # The item is still reported created (Fabric did accept the POST), but
    # the mismatch is explicit in the outcome — never silently treated as a
    # verified match.
    assert outcome.readback_status == "MISMATCH"


def test_lakehouse_readback_marked_unsupported():
    package = fh.full_package()
    lakehouse = fh.artifact_of(package, DeployableTargetType.LAKEHOUSE)
    client = fh.make_client()
    outcome = client.deploy_artifact(lakehouse)
    assert outcome.readback_status == "UNSUPPORTED"


# ── 7. Non-deployable artifact blocks deployment (service level) ──


def test_deployment_service_blocks_on_non_deployable(tmp_path, monkeypatch):
    import src.database as db_module
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.approvals import approval_service as appr
    from src.artifacts import write_package
    from src.config import get_settings
    from src.connectors.adf_source import FixtureADFSource
    from src.database import Base
    from src.migration.assessment import ADFCompatibilityAssessment
    from src.migration.deployment import DeploymentService
    from src.migration.discovery import ADFDiscoveryService
    from src.migration.plan_store import save_plan
    from src.migration.planner import MigrationPlanner
    from src.models.schemas import DeploymentMode, DeploymentStatus, DeploymentStepStatus

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
    try:
        inv = FixtureADFSource(FIXTURES).load_inventory()
        result = ADFDiscoveryService(inv).scan_inventory()
        assessment = ADFCompatibilityAssessment(inv).assess_discovery(result)
        plan = MigrationPlanner(inv).generate_plan(result, assessment, 1)
        write_package(plan.generated_package, gen)
        rec = save_plan(plan, assessment_id=1)
        ap = appr.request_approval(rec["id"], "alice")
        appr.approve(ap.approval_id, "bob")

        svc = DeploymentService(fabric_client=fh.make_client(transport=fh.FakeFabricTransport()))
        out = svc.deploy(rec["id"], ap.approval_id, DeploymentMode.REAL)

        assert out.status == DeploymentStatus.PARTIAL
        dataflow_steps = [
            s for s in out.steps if s.target_item_type == DeployableTargetType.DATAFLOW_GEN2.value
        ]
        assert dataflow_steps and dataflow_steps[0].status == DeploymentStepStatus.FAILED
        assert getattr(dataflow_steps[0], "non_deployable", False) is True
        # Nothing that depends on the non-deployable dataflow was faked.
        pipeline_steps = [
            s for s in out.steps if s.target_item_type == DeployableTargetType.DATA_PIPELINE.value
        ]
        assert pipeline_steps[0].status == DeploymentStepStatus.SKIPPED
    finally:
        get_settings.cache_clear()


# ── 8. Capacity state not verifiable (403), never invented ────────


def test_capacity_forbidden_reports_not_verifiable():
    t = fh.FakeFabricTransport()
    t.capacity_forbidden = True
    client = fh.make_client(transport=t)

    result = client.verify_capacity()

    assert result["state"] == "CAPACITY_STATE_NOT_VERIFIABLE"
    assert result["assigned_capacity_id"] == fh.CAP


def test_capacity_normally_reads_state():
    client = fh.make_client()
    result = client.verify_capacity()
    assert result["state"] == "Active"


# ── 9. MOCK / DRY_RUN regression (still function unchanged) ──────


def test_mock_and_dry_run_unaffected_by_real_mode_corrections(tmp_path, monkeypatch):
    import src.database as db_module
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.approvals import approval_service as appr
    from src.artifacts import write_package
    from src.config import get_settings
    from src.connectors.adf_source import FixtureADFSource
    from src.database import Base
    from src.migration.assessment import ADFCompatibilityAssessment
    from src.migration.deployment import DeploymentService
    from src.migration.discovery import ADFDiscoveryService
    from src.migration.plan_store import save_plan
    from src.migration.planner import MigrationPlanner
    from src.models.schemas import DeploymentMode, DeploymentStatus

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
    try:
        inv = FixtureADFSource(FIXTURES).load_inventory()
        result = ADFDiscoveryService(inv).scan_inventory()
        assessment = ADFCompatibilityAssessment(inv).assess_discovery(result)
        plan = MigrationPlanner(inv).generate_plan(result, assessment, 1)
        write_package(plan.generated_package, gen)
        rec = save_plan(plan, assessment_id=1)
        ap = appr.request_approval(rec["id"], "alice")
        appr.approve(ap.approval_id, "bob")

        dry = DeploymentService().deploy(rec["id"], ap.approval_id, DeploymentMode.DRY_RUN)
        assert dry.status == DeploymentStatus.SUCCEEDED

        mock = DeploymentService().deploy(rec["id"], ap.approval_id, DeploymentMode.MOCK)
        assert mock.status == DeploymentStatus.SUCCEEDED
        assert all(s.resource_id for s in mock.steps)
    finally:
        get_settings.cache_clear()


# ── 10. No delete/remove/drop methods anywhere on FabricClient ───


def test_no_delete_methods_on_fabric_client():
    for attr in dir(FabricClient):
        low = attr.lower()
        for banned in ("delete", "remove", "drop", "purge", "destroy"):
            assert banned not in low, f"unexpected method: {attr}"


# ── 11. No source ADF writes anywhere ─────────────────────────────


def test_no_adf_write_verbs_in_connectors():
    import ast

    src_root = Path(__file__).resolve().parent.parent / "src" / "connectors"
    write_verbs = {"put", "post", "patch", "delete"}
    offenders = []
    for path in src_root.glob("*adf*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr.lower() in write_verbs:
                offenders.append(f"{path.name}:{node.lineno} .{node.attr}(")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.upper() in {"PUT", "POST", "PATCH", "DELETE"}
            ):
                offenders.append(f"{path.name}:{node.lineno} method='{node.value}'")
    assert not offenders, f"ADF write verbs found: {offenders}"


# ── 12. Recursive secret redaction at any depth ───────────────────


def test_recursive_secret_redaction_at_any_depth():
    nested = {
        "level1": {
            "level2": [
                {"level3": {"client_secret": "super-secret-value"}},
                {"other": "keep-me"},
            ],
            "password": "another-secret",
        },
        "top_token": "leaked-token-value",
    }

    redacted = redact_secrets(nested)

    assert redacted["level1"]["level2"][0]["level3"]["client_secret"] == "***REDACTED***"
    assert redacted["level1"]["level2"][1]["other"] == "keep-me"
    assert redacted["level1"]["password"] == "***REDACTED***"
    assert redacted["top_token"] == "***REDACTED***"
    # Confirm the raw secret strings do not survive anywhere in the tree.
    import json
    blob = json.dumps(redacted)
    assert "super-secret-value" not in blob
    assert "another-secret" not in blob
    assert "leaked-token-value" not in blob


# ── Connection credential handling (env-var only, never from artifact) ─


def test_connection_credential_pulled_from_env_not_artifact(monkeypatch):
    """A non-managed-identity connection reads its secret only from the
    dedicated FABRIC_CONN_SECRET_{ARTIFACT_ID} env var at call time — never
    from the artifact/package/plan."""
    artifact = GeneratedArtifact(
        artifact_id="connection:sql-linked-service",
        source_reference="linked_service:sql-linked-service",
        target_type=DeployableTargetType.CONNECTION,
        target_name="sql-linked-service",
        generated_definition={
            "type": "FabricConnection",
            "name": "sql-linked-service",
            "properties": {
                "connectionType": "AzureSqlDatabase",
                "endpoint": "sql.example.com",
                "authentication": {"kind": "Key", "configured": False},
            },
        },
        content_digest="",
    )
    artifact = artifact.model_copy(
        update={"content_digest": compute_artifact_digest(artifact)}
    )

    t = fh.FakeFabricTransport()
    client = fh.make_client(transport=t)

    # Missing env var -> hard, safe stop (never falls back to a fake secret).
    with pytest.raises(FabricError):
        client.deploy_artifact(artifact)

    env_name = FabricClient._connection_secret_env_name(artifact.artifact_id)
    monkeypatch.setenv(env_name, "runtime-only-secret")
    outcome = client.deploy_artifact(artifact)
    assert outcome.status == "created"

    # The secret must never appear in the outcome or in any recorded call
    # bodies logged for assertions in this test process.
    assert "runtime-only-secret" not in repr(outcome)
