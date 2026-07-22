"""Pluggable runtime-metric collection — Phase 11.

Collects ONLY safe, structure-level metrics: execution status, column
schemas (types, not data), row counts, configured numeric/grouped totals,
null/duplicate counts, and duration. No complete customer record is ever
collected or persisted — ``RuntimeMetrics`` (src/models/schemas.py) has no
raw-row field, so this is structurally enforced, not just a convention.

Mirrors the existing mock-vs-real connector pattern (``MockFabricClient``
vs ``FabricClient``): a ``MetricsProvider`` protocol plus a deterministic,
safe default/mock implementation. A real implementation (e.g. querying a
Fabric Lakehouse SQL endpoint or an ADF pipeline's output dataset schema)
can be injected in its place without changing any calling code.
"""

from typing import Protocol

from src.models.schemas import ExecutionSide, RuntimeMetrics


class MetricsProvider(Protocol):
    """Collects safe runtime metrics for one completed execution."""

    def collect(
        self, side: ExecutionSide, pipeline_identity: str, run_id: str
    ) -> RuntimeMetrics:
        ...


class MockMetricsProvider:
    """Deterministic, safe mock metrics for both source and target runs.

    Source and target return matching values by default so an
    out-of-the-box runtime validation run is PASS; tests and callers can
    override via ``set_override`` to exercise mismatch/tolerance/missing
    scenarios.
    """

    def __init__(self):
        self.overrides: dict[str, RuntimeMetrics] = {}

    def _default(self) -> RuntimeMetrics:
        return RuntimeMetrics(
            status="Succeeded",
            schemas={
                "order_id": "string",
                "gross_amount": "decimal",
                "discount_amount": "decimal",
                "net_amount": "decimal",
                "region": "string",
            },
            total_row_count=20,
            valid_row_count=18,
            rejected_row_count=2,
            numeric_totals={"gross_amount": 5000.0, "net_amount": 4800.0},
            grouped_totals={"region": {"North": 2000.0, "South": 2800.0}},
            null_counts={"region": 0},
            duplicate_counts={"order_id": 0},
            duration_seconds=120.0,
        )

    def collect(
        self, side: ExecutionSide, pipeline_identity: str, run_id: str
    ) -> RuntimeMetrics:
        key = f"{ExecutionSide(side).value}:{pipeline_identity}"
        if key in self.overrides:
            return self.overrides[key]
        return self._default()

    def set_override(self, side: ExecutionSide, pipeline_identity: str, metrics: RuntimeMetrics) -> None:
        self.overrides[f"{ExecutionSide(side).value}:{pipeline_identity}"] = metrics
