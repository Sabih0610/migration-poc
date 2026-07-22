"""Controlled target Fabric pipeline execution — Phase 11.

Executes ONLY the single, exactly-configured Fabric Data Pipeline item
(``Settings.fabric_target_pipeline_item_id``) inside the single, exactly-
configured Fabric workspace. Reuses ``FabricClient``'s transport/token-
provider/retry mechanics for testability (no separate HTTP stack).

Design guarantees mirror ``fabric_client.py``:

* Real execution is disabled unless explicitly enabled AND fully configured
  (see ``Settings.runtime_execution_ready()``).
* No arbitrary/caller-supplied workspace or item id is ever accepted for
  execution — both are asserted against the injected ``FabricClient``'s
  configured workspace and this executor's configured item id.
* Only start-job (POST), get-job-status (GET), and a best-effort cancel
  exist. There are deliberately no create/update/delete/publish methods
  anywhere in this module.
* No credentials, tokens, or raw HTTP bodies ever reach logs or API
  responses — failures are mapped to the same sanitized ``FabricError``
  codes used elsewhere.
* Only safe run metadata (job instance id, item id, status, timestamps,
  duration) is ever returned — never row-level payloads.

All pre-execution authorization/approval/digest checks live in
``src.execution.execution_service`` — this module only knows how to run,
poll, and cancel the one configured pipeline job.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from src.connectors.fabric_client import CODE_BOUNDARY, CODE_TIMEOUT, FabricClient, FabricError

logger = logging.getLogger(__name__)

# Terminal Fabric job-instance statuses.
_TERMINAL = {"Completed", "Failed", "Cancelled", "Deduped"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TargetRunResult:
    """Safe metadata for one controlled Fabric pipeline job run."""

    job_instance_id: str
    item_id: str
    status: str
    started_at: str
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    safe_error_category: Optional[str] = None


class FabricPipelineExecutor:
    """Runs, polls, and (best-effort) cancels exactly one configured pipeline item."""

    def __init__(
        self,
        client: FabricClient,
        *,
        item_id: str,
        timeout_seconds: int = 1800,
        poll_interval_seconds: int = 10,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        if not item_id:
            raise FabricError(
                "Fabric execution configuration incomplete: ['item_id']",
                "FABRIC_EXECUTION_CONFIG_INCOMPLETE",
            )
        self._client = client
        self.item_id = item_id
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep_fn

    def _assert_item_boundary(self, item_id: str) -> None:
        """Reject any item id other than the exactly-configured one.

        No arbitrary/caller-supplied item id is ever accepted for execution.
        """
        if item_id != self.item_id:
            raise FabricError(
                "Requested pipeline item does not match the configured "
                "target execution boundary.",
                CODE_BOUNDARY,
            )

    def start_run(self, item_id: str) -> TargetRunResult:
        """Start an on-demand run of exactly the configured pipeline item."""
        self._assert_item_boundary(item_id)
        started = _now()
        path = self._client._ws(f"/items/{self.item_id}/jobs/instances?jobType=Pipeline")
        resp = self._client._request("POST", path)
        location = resp.headers.get("location") or resp.headers.get("Location")
        job_instance_id = None
        if location:
            marker = "/jobs/instances/"
            if marker in location:
                job_instance_id = location.split(marker, 1)[1].split("?")[0].split("/")[0]
        if not job_instance_id:
            data = resp.json() or {}
            job_instance_id = data.get("id")
        if not job_instance_id:
            raise FabricError(
                "Fabric did not return a job instance id.", "FABRIC_ERROR"
            )
        logger.info(
            "Started Fabric job '%s' for item '%s'.", job_instance_id, self.item_id
        )
        return TargetRunResult(
            job_instance_id=job_instance_id,
            item_id=self.item_id,
            status="NotStarted",
            started_at=started,
        )

    def get_status(self, job_instance_id: str) -> str:
        path = self._client._ws(
            f"/items/{self.item_id}/jobs/instances/{job_instance_id}"
        )
        resp = self._client._request("GET", path)
        data = resp.json() or {}
        return data.get("status") or "Unknown"

    def cancel_run(self, job_instance_id: str) -> None:
        """Best-effort cancel; failures are sanitized and never re-raised."""
        try:
            path = self._client._ws(
                f"/items/{self.item_id}/jobs/instances/{job_instance_id}/cancel"
            )
            self._client._request("POST", path)
        except FabricError as exc:
            logger.warning(
                "Best-effort cancel of Fabric job '%s' failed [%s].",
                job_instance_id, exc.code,
            )

    def run_to_terminal(self, item_id: str) -> TargetRunResult:
        """Start the configured pipeline item and poll it to a terminal
        state, applying the configured timeout with best-effort cancel."""
        result = self.start_run(item_id)
        deadline = time.monotonic() + self.timeout_seconds
        status = result.status
        while status not in _TERMINAL:
            if time.monotonic() >= deadline:
                self.cancel_run(result.job_instance_id)
                completed = _now()
                return TargetRunResult(
                    job_instance_id=result.job_instance_id,
                    item_id=result.item_id,
                    status="TimedOut",
                    started_at=result.started_at,
                    completed_at=completed,
                    duration_seconds=self.timeout_seconds,
                    safe_error_category=CODE_TIMEOUT,
                )
            self._sleep(self.poll_interval_seconds)
            status = self.get_status(result.job_instance_id)
        completed = _now()
        duration = (
            datetime.fromisoformat(completed) - datetime.fromisoformat(result.started_at)
        ).total_seconds()
        return TargetRunResult(
            job_instance_id=result.job_instance_id,
            item_id=result.item_id,
            status=status,
            started_at=result.started_at,
            completed_at=completed,
            duration_seconds=duration,
        )


def build_fabric_pipeline_executor_from_settings(
    settings, client: Optional[FabricClient] = None, **overrides
) -> FabricPipelineExecutor:
    """Build a FabricPipelineExecutor from settings, enforcing the enable gate."""
    from src.connectors.fabric_client import build_fabric_client_from_settings

    if not settings.runtime_execution_enabled:
        raise FabricError(
            "Runtime execution is disabled (set RUNTIME_EXECUTION_ENABLED=true).",
            "FABRIC_EXECUTION_DISABLED",
        )
    fabric_client = client or build_fabric_client_from_settings(settings)
    params = dict(
        item_id=settings.fabric_target_pipeline_item_id,
        timeout_seconds=settings.fabric_run_timeout_seconds,
        poll_interval_seconds=settings.runtime_poll_interval_seconds,
    )
    params.update(overrides)
    return FabricPipelineExecutor(fabric_client, **params)
