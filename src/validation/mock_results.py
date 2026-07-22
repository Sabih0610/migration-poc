"""Mock metrics provider for validation."""

from typing import Optional
from src.models.schemas import DatasetMetrics

class MockResultProvider:
    """Provides deterministic mock metrics for source (ADF) and target (Fabric)."""
    
    def __init__(self):
        # Allow test overrides
        self.overrides: dict[str, dict[str, DatasetMetrics]] = {
            "source": {},
            "target": {}
        }
    
    def _default_metrics(self) -> dict[str, DatasetMetrics]:
        return {
            "pipeline": DatasetMetrics(
                runtime_seconds=120.0,
                run_status="Succeeded",
                error=None
            ),
            "enriched_orders": DatasetMetrics(
                row_count=18,
                schema_hash="hash_enriched_v1",
                gross_total=5000.0,
                discount_total=200.0,
                net_total=4800.0
            ),
            "rejected_orders": DatasetMetrics(
                row_count=2,
                schema_hash="hash_rejected_v1"
            ),
            "customer_summary": DatasetMetrics(
                row_count=8,
                schema_hash="hash_summary_v1",
                customer_region_totals={
                    "North": 2000.0,
                    "South": 2800.0
                }
            )
        }

    def get_source_metrics(self) -> dict[str, DatasetMetrics]:
        metrics = self._default_metrics()
        for key, val in self.overrides.get("source", {}).items():
            if key in metrics:
                metrics[key] = metrics[key].model_copy(update=val.model_dump(exclude_unset=True))
            else:
                metrics[key] = val
        return metrics

    def get_target_metrics(self) -> dict[str, DatasetMetrics]:
        metrics = self._default_metrics()
        for key, val in self.overrides.get("target", {}).items():
            if key in metrics:
                metrics[key] = metrics[key].model_copy(update=val.model_dump(exclude_unset=True))
            else:
                metrics[key] = val
        return metrics
    
    def set_override(self, side: str, key: str, metrics: DatasetMetrics):
        if side not in self.overrides:
            self.overrides[side] = {}
        self.overrides[side][key] = metrics
