"""Phase 2 end-to-end verification script.

Loads the full mock inventory, validates all cross-references,
checks CSV data, and prints a summary report.

Exit code 0 = PASS, 1 = FAIL.
"""

import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.fixtures_loader import load_csv, load_mock_adf_inventory
from scripts.verify_helper import TempDatabase

def main() -> int:
    with TempDatabase():
        return _run()

def _run() -> int:
    fixtures = PROJECT_ROOT / "fixtures"
    errors: list[str] = []
    passed: list[str] = []

    print("=" * 60)
    print("  Phase 2 Verification")
    print("=" * 60)
    print()

    # ── Load inventory ───────────────────────────────────────
    try:
        inv = load_mock_adf_inventory(fixtures)
        passed.append("Inventory loaded")
    except Exception as e:
        errors.append(f"Failed to load inventory: {e}")
        print(f"FATAL: {errors[-1]}")
        return 1

    # ── Asset counts ─────────────────────────────────────────
    pl_count = len(inv.pipelines)
    ls_count = len(inv.linked_services)
    ds_count = len(inv.datasets)
    df_count = len(inv.data_flows)
    trg_count = len(inv.triggers)

    act_count = sum(len(p.properties.activities) for p in inv.pipelines)
    src_count = sum(
        len(df.properties.type_properties.sources) for df in inv.data_flows
    )
    sink_count = sum(
        len(df.properties.type_properties.sinks) for df in inv.data_flows
    )
    xform_count = sum(
        len(df.properties.type_properties.transformations)
        for df in inv.data_flows
    )

    print(f"  Pipelines:        {pl_count}")
    print(f"  Activities:       {act_count}")
    print(f"  Data Flows:       {df_count}")
    print(f"    Sources:        {src_count}")
    print(f"    Sinks:          {sink_count}")
    print(f"    Transformations:{xform_count}")
    print(f"  Datasets:         {ds_count}")
    print(f"  Linked Services:  {ls_count}")
    print(f"  Triggers:         {trg_count}")
    print()

    # ── Required counts ──────────────────────────────────────
    if pl_count < 1:
        errors.append("No pipelines found")
    else:
        passed.append(f"{pl_count} pipeline(s)")

    if df_count < 1:
        errors.append("No data flows found")
    else:
        passed.append(f"{df_count} data flow(s)")

    if src_count != 3:
        errors.append(f"Expected 3 sources, got {src_count}")
    else:
        passed.append("3 sources")

    if sink_count != 3:
        errors.append(f"Expected 3 sinks, got {sink_count}")
    else:
        passed.append("3 sinks")

    # ── Cross-reference validation ───────────────────────────
    ls_names = {ls.name for ls in inv.linked_services}
    ds_names = {ds.name for ds in inv.datasets}
    df_names = {df.name for df in inv.data_flows}
    pl_names = {pl.name for pl in inv.pipelines}
    unresolved = []

    for ds in inv.datasets:
        ref = ds.properties.linked_service_name
        if ref and ref.reference_name not in ls_names:
            unresolved.append(f"DS '{ds.name}' → LS '{ref.reference_name}'")

    for df in inv.data_flows:
        for src in df.properties.type_properties.sources:
            if src.dataset and src.dataset.reference_name not in ds_names:
                unresolved.append(
                    f"DF source '{src.name}' → DS '{src.dataset.reference_name}'"
                )
        for sink in df.properties.type_properties.sinks:
            if sink.dataset and sink.dataset.reference_name not in ds_names:
                unresolved.append(
                    f"DF sink '{sink.name}' → DS '{sink.dataset.reference_name}'"
                )

    for trg in inv.triggers:
        for pipe_ref in trg.properties.pipelines:
            if pipe_ref.pipeline_reference:
                if pipe_ref.pipeline_reference.reference_name not in pl_names:
                    unresolved.append(
                        f"Trigger '{trg.name}' → PL "
                        f"'{pipe_ref.pipeline_reference.reference_name}'"
                    )

    if unresolved:
        for u in unresolved:
            errors.append(f"Unresolved: {u}")
    else:
        passed.append("All references resolved")

    # ── Data flow transformations ────────────────────────────
    if inv.data_flows:
        xform_names = {
            t.name for t in inv.data_flows[0].properties.type_properties.transformations
        }
        for required in [
            "JoinCustomers",
            "JoinProducts",
            "DerivedColumns",
            "ConditionalSplitValidRejected",
            "AggregateByCustomerRegion",
        ]:
            if required in xform_names:
                passed.append(f"Transformation: {required}")
            else:
                errors.append(f"Missing transformation: {required}")

    # ── CSV data ─────────────────────────────────────────────
    orders = load_csv(fixtures / "data" / "orders.csv")
    customers = load_csv(fixtures / "data" / "customers.csv")
    products = load_csv(fixtures / "data" / "products.csv")

    if orders and len(orders) > 0:
        passed.append(f"Orders: {len(orders)} rows")
    else:
        errors.append("Orders CSV empty or missing")

    if customers and len(customers) > 0:
        passed.append(f"Customers: {len(customers)} rows")
    else:
        errors.append("Customers CSV empty or missing")

    if products and len(products) > 0:
        passed.append(f"Products: {len(products)} rows")
    else:
        errors.append("Products CSV empty or missing")

    # Invalid orders check
    if orders:
        quantities = [int(r["Quantity"]) for r in orders]
        invalid = [q for q in quantities if q <= 0]
        if invalid:
            passed.append(f"Invalid quantities found: {invalid}")
        else:
            errors.append("No invalid quantities in orders (expected some)")

    # ── Secret scanning ──────────────────────────────────────
    secret_patterns = [
        "password", "client_secret", "accountKey", "connectionString",
        "accessToken", "servicePrincipalKey",
    ]
    secrets_found = []
    for json_file in (fixtures / "adf").rglob("*.json"):
        content = json_file.read_text(encoding="utf-8")
        for pat in secret_patterns:
            if pat in content:
                secrets_found.append(f"{pat} in {json_file.name}")

    if secrets_found:
        for s in secrets_found:
            errors.append(f"Secret found: {s}")
    else:
        passed.append("No secrets in fixtures")

    # ── Report ───────────────────────────────────────────────
    print()
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
        print(f"  RESULT: FAIL")
        return 1
    else:
        print(f"  FAILED: 0")
        print()
        print(f"  RESULT: PASS")
        return 0


if __name__ == "__main__":
    sys.exit(main())
