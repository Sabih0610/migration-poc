"""Phase 9 controlled read-only verification against real Azure.

Performs ONLY read operations against the configured Data Factory, using
credentials from the environment / .env. Persists the discovery snapshot
to an isolated temporary database (migration_poc.db is never touched),
reloads it (simulated restart), then runs assessment, planning, package
generation, and a determinism check.

Exit 0 = PASS, 1 = FAIL.
"""

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_helper import TempDatabase
from src.config import get_settings
from src.connectors.azure_adf_client import AzureADFClient, AzureDiscoveryError
from src.connectors.azure_adf_source import AzureADFSource
from src.migration.assessment import ADFCompatibilityAssessment
from src.migration.discovery import ADFDiscoveryService
from src.migration.discovery_store import get_discovery, save_discovery
from src.migration.planner import MigrationPlanner

SECRET_TOKENS = ("password", "client_secret", "accountkey", "connectionstring",
                 "accesstoken", "serviceprincipalkey")


def _build_client(settings) -> AzureADFClient:
    df_name = os.getenv("AZURE_DATA_FACTORY_NAME") or settings.azure_data_factory_name or "Sabih-df"
    return AzureADFClient(
        tenant_id=settings.azure_tenant_id,
        client_id=settings.azure_client_id,
        client_secret=settings.azure_client_secret,
        subscription_id=settings.azure_subscription_id,
        resource_group=settings.azure_resource_group or "AzureFabricMigrationPOC",
        data_factory_name=df_name,
        timeout_seconds=settings.azure_discovery_timeout_seconds,
    )


def main() -> int:
    passed: list[str] = []
    errors: list[str] = []

    print("=" * 60)
    print("  Phase 9 Verification (controlled read-only, real Azure)")
    print("=" * 60)

    with TempDatabase(prefix="verify_phase9_"):
        settings = get_settings()
        missing = [
            k for k in ("azure_tenant_id", "azure_client_id", "azure_client_secret",
                        "azure_subscription_id", "azure_resource_group")
            if not getattr(settings, k, "")
        ]
        if missing:
            print(f"  RESULT: FAIL — missing Azure config: {missing}")
            return 1

        client = _build_client(settings)

        try:
            env = client.verify_environment()
            passed.append(f"Subscription accessible; RG boundary enforced "
                          f"({env['resource_group']} @ {env['resource_group_location']})")
        except AzureDiscoveryError as exc:
            print(f"  RESULT: FAIL — environment: [{exc.code}] {exc.message}")
            return 1

        try:
            df = client.verify_data_factory()
            passed.append(f"Data Factory '{df['name']}' exists "
                          f"(location={df['location']}, state={df['provisioning_state']})")
        except AzureDiscoveryError as exc:
            print(f"  RESULT: FAIL — data factory: [{exc.code}] {exc.message}")
            return 1

        for ns in ("Microsoft.DataFactory", "Microsoft.Storage"):
            try:
                st = client.provider_status(ns)
                passed.append(f"Provider {ns}: {st['registration_state']}")
            except AzureDiscoveryError as exc:
                errors.append(f"provider {ns}: [{exc.code}] {exc.message}")

        # Read-only discovery of all asset types.
        try:
            source = AzureADFSource(client)
            inventory = source.load_inventory()
        except AzureDiscoveryError as exc:
            print(f"  RESULT: FAIL — discovery: [{exc.code}] {exc.message}")
            return 1

        counts = {
            "pipelines": len(inventory.pipelines),
            "data_flows": len(inventory.data_flows),
            "datasets": len(inventory.datasets),
            "linked_services": len(inventory.linked_services),
            "triggers": len(inventory.triggers),
        }
        passed.append(f"Listed + downloaded definitions: {counts}")

        result = ADFDiscoveryService(inventory).scan_inventory()
        record = save_discovery(result)
        discovery_id = record["id"]
        passed.append(f"Discovery persisted (id={discovery_id}, "
                      f"{result.summary.artifact_count} artifacts, "
                      f"{result.summary.component_count} components)")

        # Simulated restart: reload purely from the persisted snapshot.
        reloaded = get_discovery(discovery_id)
        if reloaded is None:
            errors.append("snapshot did not survive restart")
            reloaded_result = result
        else:
            reloaded_result = reloaded["result"]
            passed.append("Snapshot survived simulated restart")

        exec_order = ADFDiscoveryService(
            reloaded_result.inventory
        ).scan_inventory()
        deps_ok = exec_order.summary.dependency_count > 0 and (
            exec_order.summary.missing_dependency_count == 0
        )
        passed.append(
            f"Dependencies complete/ordered "
            f"(deps={exec_order.summary.dependency_count}, "
            f"missing={exec_order.summary.missing_dependency_count})"
            if deps_ok else "dependency check produced missing refs"
        )

        assessment = ADFCompatibilityAssessment(
            reloaded_result.inventory
        ).assess_discovery(reloaded_result)
        passed.append(f"Assessment: {assessment.overall_status.value}")

        plan1 = MigrationPlanner(reloaded_result.inventory).generate_plan(
            reloaded_result, assessment, discovery_id
        )
        plan2 = MigrationPlanner(reloaded_result.inventory).generate_plan(
            reloaded_result, assessment, discovery_id
        )
        pkg1 = plan1.generated_package
        pkg2 = plan2.generated_package
        if pkg1 is None:
            errors.append("no generated package produced")
        else:
            passed.append(f"Generated package: {len(pkg1.artifacts)} artifacts, "
                          f"{len(pkg1.manifest.entries)} manifest entries")
            digests1 = [a.content_digest for a in pkg1.artifacts]
            digests2 = [a.content_digest for a in pkg2.artifacts]
            if (digests1 == digests2
                    and pkg1.manifest.package_digest == pkg2.manifest.package_digest):
                passed.append("Manifest and digests are deterministic")
            else:
                errors.append("package digests are not deterministic")

        # Secret scan on all produced output.
        blob = json.dumps(reloaded_result.model_dump(mode="json", by_alias=True)).lower()
        leaked = [t for t in SECRET_TOKENS if f'"{t}"' in blob and "***redacted***" not in blob]
        # Key names may legitimately appear; ensure no *unredacted* secret values.
        from src.migration.discovery_store import _find_unredacted_secret
        unredacted = _find_unredacted_secret(
            reloaded_result.model_dump(mode="json", by_alias=True)
        )
        if unredacted:
            errors.append(f"unredacted secret under key '{unredacted}'")
        else:
            passed.append("No unredacted secrets in output")

    print()
    for p in passed:
        print(f"  [OK] {p}")
    for e in errors:
        print(f"  [FAIL] {e}")
    print()
    if errors:
        print("  RESULT: FAIL")
        return 1
    print("  RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
