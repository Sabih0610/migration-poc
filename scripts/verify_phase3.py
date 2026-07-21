"""Phase 3 verification script."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.api.app import app
from src.fixtures_loader import load_mock_adf_inventory
from src.migration.dependency_graph import DependencyGraph
from src.migration.discovery import ADFDiscoveryService


from scripts.verify_helper import TempDatabase


def main() -> int:
    with TempDatabase():
        return _run()

def _run() -> int:
    fixtures = PROJECT_ROOT / "fixtures"
    errors = []
    passed = []

    print("=" * 60)
    print("  Phase 3 Verification")
    print("=" * 60)
    print()

    # Load inventory
    try:
        inv = load_mock_adf_inventory(fixtures)
        passed.append("Inventory loaded")
    except Exception as e:
        errors.append(f"Failed to load inventory: {e}")
        return 1

    # Discovery complete
    try:
        svc = ADFDiscoveryService(inv)
        res = svc.scan_inventory()
        passed.append("Discovery completed")
    except Exception as e:
        errors.append(f"Discovery failed: {e}")
        return 1

    # Expected assets found
    if len(res.assets) > 0:
        passed.append(f"Expected assets found: {len(res.assets)}")
    else:
        errors.append("No assets found")

    # Expected dependencies found
    if len(res.dependencies) > 0:
        passed.append(f"Expected dependencies found: {len(res.dependencies)}")
    else:
        errors.append("No dependencies found")

    # Zero missing references
    if len(res.missing_dependencies) == 0:
        passed.append("Zero missing references")
    else:
        errors.append(f"Found missing references: {len(res.missing_dependencies)}")

    # Zero cycles
    graph = DependencyGraph()
    graph.build_graph(res)
    cycles = graph.detect_cycles()
    if len(cycles) == 0:
        passed.append("Zero cycles")
    else:
        errors.append(f"Found cycles: {len(cycles)}")

    # Execution order generated
    order = graph.get_execution_order()
    if len(order) > 0:
        passed.append(f"Execution order generated: {len(order)} nodes")
    else:
        errors.append("Execution order empty or cycle prevented it")

    # API scan works
    client = TestClient(app)
    resp = client.post("/api/discovery/scan")
    if resp.status_code == 200:
        passed.append("API scan works")
    else:
        errors.append(f"API scan failed: {resp.status_code} {resp.text}")

    # Summary counts correct
    summary = res.summary
    if summary.pipeline_count == 1 and summary.data_flow_count == 1:
        passed.append("Summary counts correct")
    else:
        errors.append("Summary counts incorrect")

    # No secrets in output
    import json

    serialized = json.dumps(res.model_dump(), default=str)
    secrets = ["password", "client_secret", "accountKey", "connectionString"]
    found_secrets = [s for s in secrets if s in serialized]
    if not found_secrets:
        passed.append("No secrets in discovery results")
    else:
        errors.append(f"Secrets found: {found_secrets}")

    print("-" * 60)
    print(f"  PASSED: {len(passed)}")
    for p in passed:
        print(f"    [OK] {p}")
    print()

    if errors:
        print(f"  FAILED: {len(errors)}")
        for e in errors:
            print(f"    [FAIL] {e}")
        print()
        print("  RESULT: FAIL")
        return 1
    else:
        print(f"  FAILED: 0")
        print()
        print("  RESULT: PASS")
        return 0


if __name__ == "__main__":
    sys.exit(main())
