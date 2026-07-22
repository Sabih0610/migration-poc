"""Verify the artifact-definition discovery foundation.

Uses an isolated database/generated/report workspace and performs no cloud
calls. Exit code 0 = PASS, 1 = FAIL.
"""

import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import inspect

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_helper import TempDatabase
from src.api.app import app
from src.migration.dependency_graph import DependencyGraph
from src.migration.discovery_store import get_latest_discovery


def main() -> int:
    print("=" * 60)
    print("  Phase 8 Artifact Foundation Verification")
    print("=" * 60)

    with TempDatabase(prefix="verify_phase8_foundation_") as workspace:
        return _run(workspace)


def _run(workspace: TempDatabase) -> int:
    errors: list[str] = []
    client = TestClient(app)

    scan = client.post("/api/discovery/scan")
    if scan.status_code != 200:
        errors.append(f"Discovery API failed: {scan.status_code} {scan.text}")
        return _finish(errors)

    record = get_latest_discovery()
    if record is None:
        errors.append("Discovery snapshot was not persisted")
        return _finish(errors)

    result = record["result"]
    summary = result.summary
    if summary.artifact_count != 10:
        errors.append(f"Expected 10 artifacts, got {summary.artifact_count}")
    if summary.component_count != 11:
        errors.append(f"Expected 11 components, got {summary.component_count}")
    if summary.activity_count != 4:
        errors.append(f"Expected 4 nested activities, got {summary.activity_count}")
    if summary.transformation_count != 7:
        errors.append(
            f"Expected 7 transformations, got {summary.transformation_count}"
        )

    # Exact source JSON is embedded in the persisted inventory.
    pipeline = result.inventory.source_definitions["pipelines"][0]
    if "parameters" not in pipeline["properties"]:
        errors.append("Pipeline parameters were not preserved")
    if "variables" not in pipeline["properties"]:
        errors.append("Pipeline variables were not preserved")
    if len(result.expressions) != 2:
        errors.append(f"Expected 2 expressions, got {len(result.expressions)}")
    if len(result.connection_references) != 6:
        errors.append(
            "Expected 6 connection references, got "
            f"{len(result.connection_references)}"
        )

    graph = DependencyGraph()
    graph.build_graph(result)
    order = graph.get_execution_order()
    positions = {name: index for index, name in enumerate(order)}
    for edge in result.dependencies:
        if positions[edge.target] >= positions[edge.source]:
            errors.append(
                f"Dependency order invalid: {edge.target} !< {edge.source}"
            )

    tables = set(inspect(workspace.engine).get_table_names())
    required_tables = {
        "discovery_runs",
        "validation_runs",
        "structural_validation_runs",
        "runtime_validation_runs",
    }
    missing_tables = required_tables - tables
    if missing_tables:
        errors.append(f"Missing database tables: {sorted(missing_tables)}")

    # A second TestClient proves retrieval is DB-backed rather than a cache.
    latest = TestClient(app).get("/api/discovery/latest")
    if latest.status_code != 200 or latest.json()["discovery_id"] != record["id"]:
        errors.append("Persisted discovery did not survive client restart")

    print(f"  Artifacts:  {summary.artifact_count}")
    print(f"  Components: {summary.component_count}")
    print("  Dependency order:")
    print("    " + " -> ".join(order))
    return _finish(errors)


def _finish(errors: list[str]) -> int:
    if errors:
        for error in errors:
            print(f"  [FAIL] {error}")
        print("  RESULT: FAIL")
        return 1
    print("  RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
