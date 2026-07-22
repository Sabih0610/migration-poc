"""Controlled source Azure Data Factory pipeline execution — Phase 11.

Executes ONLY the single, exactly-configured pipeline
(``Settings.adf_source_pipeline_name``) inside the single, exactly-configured
Data Factory (subscription/resource-group/factory name reused from Phase 9's
read-only discovery settings). Design guarantees mirror
``azure_adf_client.py``:

* Credentials come only from configuration (environment variables).
* Real execution is disabled unless explicitly enabled AND fully configured
  (see ``Settings.runtime_execution_ready()``).
* No free-form pipeline name is ever accepted from a caller — any name that
  does not exactly equal the configured pipeline name is rejected before any
  network call.
* Only createRun (POST), get-run-status (GET), and a best-effort cancel
  exist. There are deliberately no definition update/publish/trigger/
  provider-registration/delete methods anywhere in this module.
* No credentials, tokens, or raw SDK error text ever reach logs or API
  responses — failures are mapped to stable, sanitized codes.
* Only safe run metadata (run id, pipeline name, status, timestamps,
  duration) is ever returned — never row-level payloads.

The Azure SDK client is injected so unit tests can supply fakes and never
touch the network.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Error codes surfaced to callers (never include raw SDK detail).
CODE_DISABLED = "ADF_EXECUTION_DISABLED"
CODE_CONFIG = "ADF_EXECUTION_CONFIG_INCOMPLETE"
CODE_BOUNDARY = "ADF_EXECUTION_BOUNDARY_VIOLATION"
CODE_AUTH = "ADF_EXECUTION_AUTH_FAILED"
CODE_AUTHZ = "ADF_EXECUTION_AUTHORIZATION_FAILED"
CODE_NOT_FOUND = "ADF_EXECUTION_NOT_FOUND"
CODE_TIMEOUT = "ADF_EXECUTION_TIMEOUT"
CODE_THROTTLED = "ADF_EXECUTION_THROTTLED"
CODE_UNKNOWN = "ADF_EXECUTION_ERROR"

# Terminal ADF pipeline-run statuses.
_TERMINAL = {"Succeeded", "Failed", "Cancelled"}


class AzureExecutionError(Exception):
    """Sanitized, controlled-execution Azure failure with a stable code."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class SourceRunResult:
    """Safe metadata for one controlled ADF pipeline run."""

    run_id: str
    pipeline_name: str
    status: str
    started_at: str
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    safe_error_category: Optional[str] = None


def _default_credential_factory(tenant_id, client_id, client_secret):
    from azure.identity import ClientSecretCredential

    return ClientSecretCredential(
        tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
    )


def _default_datafactory_factory(credential, subscription_id):
    from azure.mgmt.datafactory import DataFactoryManagementClient

    return DataFactoryManagementClient(credential, subscription_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AzureADFExecutor:
    """Runs, polls, and (best-effort) cancels exactly one configured pipeline."""

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        subscription_id: str,
        resource_group: str,
        data_factory_name: str,
        pipeline_name: str,
        timeout_seconds: int = 1800,
        poll_interval_seconds: int = 10,
        credential_factory: Optional[Callable] = None,
        datafactory_client_factory: Optional[Callable] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        missing = [
            n
            for n, v in {
                "tenant_id": tenant_id,
                "client_id": client_id,
                "client_secret": client_secret,
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "data_factory_name": data_factory_name,
                "pipeline_name": pipeline_name,
            }.items()
            if not v
        ]
        if missing:
            raise AzureExecutionError(
                f"ADF execution configuration incomplete: {missing}", CODE_CONFIG
            )

        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret  # never logged
        self.subscription_id = subscription_id
        self.resource_group = resource_group
        self.data_factory_name = data_factory_name
        self.pipeline_name = pipeline_name
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep_fn

        self._credential_factory = credential_factory or _default_credential_factory
        self._datafactory_factory = (
            datafactory_client_factory or _default_datafactory_factory
        )
        self._credential = None
        self._df_client = None

    def _datafactory(self):
        if self._df_client is None:
            if self._credential is None:
                self._credential = self._credential_factory(
                    self._tenant_id, self._client_id, self._client_secret
                )
            self._df_client = self._datafactory_factory(
                self._credential, self.subscription_id
            )
        return self._df_client

    # ── Error mapping (sanitized) ────────────────────────────────

    def _call(self, op_name: str, fn: Callable):
        try:
            return fn()
        except AzureExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberately broad, sanitized
            code = self._classify(exc)
            logger.warning(
                "ADF execution '%s' failed [%s] (%s)",
                op_name, code, type(exc).__name__,
            )
            raise AzureExecutionError(
                f"ADF execution operation '{op_name}' failed.", code
            ) from None

    @staticmethod
    def _classify(exc: Exception) -> str:
        status = getattr(exc, "status_code", None)
        name = type(exc).__name__
        if name == "ClientAuthenticationError" or status == 401:
            return CODE_AUTH
        if status == 403:
            return CODE_AUTHZ
        if name == "ResourceNotFoundError" or status == 404:
            return CODE_NOT_FOUND
        if status == 429:
            return CODE_THROTTLED
        if name in ("ServiceRequestError", "ServiceResponseTimeoutError") or isinstance(
            exc, TimeoutError
        ):
            return CODE_TIMEOUT
        return CODE_UNKNOWN

    # ── Boundary enforcement ─────────────────────────────────────

    def _assert_pipeline_boundary(self, pipeline_name: str) -> None:
        """Reject any pipeline name other than the exactly-configured one.

        No free-form pipeline parameter is ever accepted from callers.
        """
        if pipeline_name != self.pipeline_name:
            raise AzureExecutionError(
                "Requested pipeline does not match the configured source "
                "pipeline boundary.",
                CODE_BOUNDARY,
            )

    # ── Controlled execution (createRun / get / cancel only) ──────

    def start_run(self, pipeline_name: str) -> SourceRunResult:
        """Start a run of exactly the configured pipeline. No other pipeline
        name, no parameters beyond the pipeline's own defaults, is ever
        accepted."""
        self._assert_pipeline_boundary(pipeline_name)
        client = self._datafactory()
        started = _now()
        response = self._call(
            "pipelines.create_run",
            lambda: client.pipelines.create_run(
                self.resource_group, self.data_factory_name, self.pipeline_name
            ),
        )
        run_id = getattr(response, "run_id", None) or (
            response.get("run_id") if isinstance(response, dict) else None
        )
        if not run_id:
            raise AzureExecutionError(
                "ADF did not return a pipeline run id.", CODE_UNKNOWN
            )
        logger.info(
            "Started ADF run '%s' for pipeline '%s'.", run_id, self.pipeline_name
        )
        return SourceRunResult(
            run_id=run_id,
            pipeline_name=self.pipeline_name,
            status="InProgress",
            started_at=started,
        )

    def get_status(self, run_id: str) -> str:
        client = self._datafactory()
        run = self._call(
            "pipeline_runs.get",
            lambda: client.pipeline_runs.get(
                self.resource_group, self.data_factory_name, run_id
            ),
        )
        return getattr(run, "status", None) or "Unknown"

    def cancel_run(self, run_id: str) -> None:
        """Best-effort cancel; failures are sanitized and never re-raised
        to the caller (used only for timeout handling cleanup)."""
        try:
            client = self._datafactory()
            self._call(
                "pipeline_runs.cancel",
                lambda: client.pipeline_runs.cancel(
                    self.resource_group, self.data_factory_name, run_id
                ),
            )
        except AzureExecutionError as exc:
            logger.warning(
                "Best-effort cancel of ADF run '%s' failed [%s].", run_id, exc.code
            )

    def run_to_terminal(self, pipeline_name: str) -> SourceRunResult:
        """Start the configured pipeline and poll it to a terminal state,
        applying the configured timeout with best-effort cancellation."""
        result = self.start_run(pipeline_name)
        deadline = time.monotonic() + self.timeout_seconds
        status = result.status
        while status not in _TERMINAL:
            if time.monotonic() >= deadline:
                self.cancel_run(result.run_id)
                completed = _now()
                return SourceRunResult(
                    run_id=result.run_id,
                    pipeline_name=result.pipeline_name,
                    status="TimedOut",
                    started_at=result.started_at,
                    completed_at=completed,
                    duration_seconds=self.timeout_seconds,
                    safe_error_category=CODE_TIMEOUT,
                )
            self._sleep(self.poll_interval_seconds)
            status = self.get_status(result.run_id)
        completed = _now()
        duration = (
            datetime.fromisoformat(completed) - datetime.fromisoformat(result.started_at)
        ).total_seconds()
        return SourceRunResult(
            run_id=result.run_id,
            pipeline_name=result.pipeline_name,
            status=status,
            started_at=result.started_at,
            completed_at=completed,
            duration_seconds=duration,
        )


def build_azure_adf_executor_from_settings(settings, **overrides) -> AzureADFExecutor:
    """Build an AzureADFExecutor from settings, enforcing the enable gate."""
    if not settings.runtime_execution_enabled:
        raise AzureExecutionError(
            "Runtime execution is disabled (set RUNTIME_EXECUTION_ENABLED=true).",
            CODE_DISABLED,
        )
    params = dict(
        tenant_id=settings.azure_tenant_id,
        client_id=settings.azure_client_id,
        client_secret=settings.azure_client_secret,
        subscription_id=settings.azure_subscription_id,
        resource_group=settings.azure_resource_group,
        data_factory_name=settings.azure_data_factory_name,
        pipeline_name=settings.adf_source_pipeline_name,
        timeout_seconds=settings.adf_run_timeout_seconds,
        poll_interval_seconds=settings.runtime_poll_interval_seconds,
    )
    params.update(overrides)
    return AzureADFExecutor(**params)
