"""Fakes for the Fabric REST transport — Phase 10 tests (no network)."""

from pathlib import Path
from typing import Optional

from src.artifacts import compute_artifact_digest
from src.connectors.adf_source import FixtureADFSource
from src.connectors.fabric_client import FabricClient, _Response
from src.migration.assessment import ADFCompatibilityAssessment
from src.migration.discovery import ADFDiscoveryService
from src.migration.planner import MigrationPlanner

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

WS = "ws-11111111"
CAP = "cap-22222222"
CLIENT_ID = "spn-client-id"
FAKE_TOKEN = "FAKE-TOKEN-should-never-leak"


def full_package():
    """Build the full fixture generated package (all artifact types)."""
    inv = FixtureADFSource(FIXTURES).load_inventory()
    result = ADFDiscoveryService(inv).scan_inventory()
    assessment = ADFCompatibilityAssessment(inv).assess_discovery(result)
    return MigrationPlanner(inv).generate_plan(result, assessment, 1).generated_package


def artifact_of(package, target_type):
    for artifact in package.artifacts:
        if artifact.target_type == target_type:
            return artifact
    raise AssertionError(f"no artifact of type {target_type}")


def deployable_dataflow_artifact(package):
    """Return a copy of the fixture Dataflow Gen2 artifact with a synthetic
    'compiledPowerQueryMashup' property injected, so it round-trips through
    the adapter as deployable. Used only to exercise the deployable path in
    tests; this codebase has no real MDF -> Power Query converter, and
    without this synthetic property the adapter correctly marks the
    fixture's dataflow NON_DEPLOYABLE (see test_phase10_corrected.py)."""
    from src.models.schemas import DeployableTargetType

    dataflow = artifact_of(package, DeployableTargetType.DATAFLOW_GEN2)
    definition = dict(dataflow.generated_definition)
    properties = dict(definition.get("properties", {}))
    properties["compiledPowerQueryMashup"] = (
        "section Section1;\nshared Query1 = let Source = 1 in Source;"
    )
    definition["properties"] = properties
    updated = dataflow.model_copy(
        update={"generated_definition": definition, "content_digest": ""}
    )
    digest = compute_artifact_digest(updated)
    return updated.model_copy(update={"content_digest": digest})


class FakeFabricTransport:
    """Programmable in-memory Fabric REST transport.

    Records calls, serves workspace/capacity/role/items reads, and
    supports create-then-reuse so idempotent reruns are exercised.

    Also fakes the *non*-workspace-item Fabric surfaces the corrected
    client uses: the tenant-scoped Connections API (``/connections``),
    getDefinition read-back (``/items/{id}/getDefinition``), and the Job
    Scheduler API attached to a pipeline item
    (``/items/{id}/jobs/{type}/schedules``).
    """

    def __init__(self):
        self.calls: list[tuple] = []
        self.items: list[dict] = []
        self.connections: list[dict] = []
        # pipeline_item_id -> list[schedule dict]
        self.schedules: dict[str, list[dict]] = {}
        # item_id -> definition dict stored at create time (for getDefinition)
        self.definitions: dict[str, dict] = {}
        self.workspace = {"id": WS, "displayName": "Target WS", "capacityId": CAP}
        self.capacity = {"id": CAP, "state": "Active"}
        self.role_assignments = {
            "value": [{"role": "Admin", "principal": {"id": CLIENT_ID}}]
        }
        self.tenant_settings = {
            "tenantSettings": [
                {"settingName": "ServicePrincipalAccess", "enabled": True}
            ]
        }
        self.create_status = 201
        self.force = None  # (path_substring, status) -> error injection
        self.timeout_always = False
        self.throttle_times = 0  # return 429 this many times then succeed
        self.fail_display_name = None  # POST for this displayName -> 500
        self.capacity_forbidden = False  # simulate 403 reading /capacities/{id}
        # If set, getDefinition returns THIS instead of the stored definition
        # (used to simulate a read-back mismatch).
        self.readback_override: Optional[dict] = None
        self._id = 100
        self._sched_id = 100
        self._throttled = 0
        # ── Phase 11: controlled pipeline execution (Job Scheduler
        # "run on demand" instances), kept separate from the recurrence
        # schedules above.
        self.job_status_sequence: list[str] = ["Completed"]
        self.job_instance_statuses: dict[str, list[str]] = {}
        self.job_cancel_calls: list[str] = []
        self._job_id = 100

    def __call__(self, method, url, headers=None, json_body=None):
        self.calls.append((method, url, headers, json_body))
        path = url
        if self.timeout_always:
            raise TimeoutError("simulated timeout")
        if self.force and self.force[0] in path:
            return _Response(self.force[1], {"error": {"code": "x"}}, {})
        if self.capacity_forbidden and "/capacities/" in path:
            return _Response(403, {"error": {"code": "Forbidden"}}, {})

        # ── Connections (tenant-scoped, NOT a workspace item) ───────
        if method == "POST" and path.endswith("/connections"):
            self._id += 1
            new = {
                "id": f"conn-{self._id}",
                "displayName": json_body["displayName"],
            }
            self.connections.append(new)
            return _Response(self.create_status, {"id": new["id"]}, {})
        if method == "GET" and path.endswith("/connections"):
            return _Response(200, {"value": self.connections}, {})

        # ── getDefinition read-back ──────────────────────────────────
        if method == "POST" and path.endswith("/getDefinition"):
            item_id = path.split("/items/")[1].split("/getDefinition")[0]
            definition = self.readback_override
            if definition is None:
                definition = self.definitions.get(item_id, {"parts": []})
            return _Response(200, {"definition": definition}, {})

        # ── Job Scheduler "run on demand" instances (Phase 11 execution) ──
        if method == "POST" and path.split("?")[0].endswith("/jobs/instances"):
            if self.throttle_times and self._throttled < self.throttle_times:
                self._throttled += 1
                return _Response(429, None, {"retry-after": "0"})
            self._job_id += 1
            job_id = f"job-{self._job_id}"
            self.job_instance_statuses[job_id] = list(self.job_status_sequence)
            return _Response(
                202, {}, {"location": f"/items/x/jobs/instances/{job_id}"}
            )
        if method == "POST" and "/jobs/instances/" in path and path.endswith("/cancel"):
            job_id = path.split("/jobs/instances/")[1].split("/cancel")[0]
            self.job_cancel_calls.append(job_id)
            return _Response(200, {}, {})
        if method == "GET" and "/jobs/instances/" in path:
            job_id = path.split("/jobs/instances/")[1]
            statuses = self.job_instance_statuses.get(job_id, ["Completed"])
            status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
            return _Response(200, {"status": status}, {})

        # ── Job Scheduler (attached to a pipeline item) ──────────────
        if "/jobs/" in path and "/schedules" in path:
            pipeline_item_id = path.split("/items/")[1].split("/jobs/")[0]
            if method == "POST":
                self._sched_id += 1
                sched = {
                    "id": f"sched-{self._sched_id}",
                    "displayName": json_body["displayName"],
                    "enabled": json_body.get("enabled", False),
                }
                self.schedules.setdefault(pipeline_item_id, []).append(sched)
                return _Response(self.create_status, {"id": sched["id"]}, {})
            if method == "GET":
                return _Response(
                    200, {"value": self.schedules.get(pipeline_item_id, [])}, {}
                )

        # ── Workspace items ───────────────────────────────────────────
        if method == "POST" and path.endswith("/items"):
            if self.fail_display_name and json_body.get("displayName") == self.fail_display_name:
                return _Response(500, {"error": {"code": "InternalError"}}, {})
            if self.throttle_times and self._throttled < self.throttle_times:
                self._throttled += 1
                return _Response(429, None, {"retry-after": "0"})
            self._id += 1
            new = {
                "id": f"item-{self._id}",
                "type": json_body["type"],
                "displayName": json_body["displayName"],
                "workspaceId": WS,
            }
            self.items.append(new)
            self.definitions[new["id"]] = json_body.get("definition", {"parts": []})
            return _Response(self.create_status, {"id": new["id"]}, {})
        if method == "GET" and path.endswith("/items"):
            return _Response(200, {"value": self.items}, {})
        if method == "GET" and path.endswith("/roleAssignments"):
            return _Response(200, self.role_assignments, {})
        if method == "GET" and "/capacities/" in path:
            return _Response(200, self.capacity, {})
        if method == "GET" and "/admin/tenantsettings" in path:
            return _Response(200, self.tenant_settings, {})
        if method == "GET" and path.endswith(f"/workspaces/{WS}"):
            return _Response(200, self.workspace, {})
        return _Response(404, {"error": "not found"}, {})


def make_client(transport=None, token_provider=None, **overrides):
    params = dict(
        tenant_id="t",
        client_id=CLIENT_ID,
        client_secret="FABRIC-SECRET-should-never-leak",
        workspace_id=WS,
        capacity_id=CAP,
        token_provider=token_provider or (lambda: FAKE_TOKEN),
        transport=transport or FakeFabricTransport(),
        sleep_fn=lambda _s: None,
    )
    params.update(overrides)
    return FabricClient(**params)
