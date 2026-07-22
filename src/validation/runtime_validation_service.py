"""RuntimeValidationService — Phase 11.

Compares the safe runtime metrics collected from a controlled source
execution and a controlled target execution. Strictly additive: this
service never reads or writes structural validation records, and a
runtime FAIL can coexist with a structural PASS (and vice versa).

Rule semantics:

* Missing metrics never silently pass — they produce an explicit
  INCONCLUSIVE check.
* Source or target execution failure makes the whole result FAIL.
* Row-count / schema / numeric-total / grouped-total mismatches (outside
  configured tolerance) make the whole result FAIL.
* A duration-only deviation (when configured to allow it) makes the
  result PASS_WITH_WARNINGS instead of FAIL.
* Every mismatch/inconclusive check carries a human-readable explanation.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from src.artifacts import ArtifactPackageError
from src.config import get_settings
from src.execution.execution_store import get_execution
from src.migration.deployment_store import get_deployment
from src.migration.plan_store import (
    compute_plan_package_fingerprint,
    get_plan,
    verify_plan_package,
)
from src.models.schemas import (
    DeploymentMode,
    DeploymentStatus,
    ExecutionSide,
    ExecutionStatus,
    RuntimeMetrics,
    RuntimeValidationCheckResult,
    RuntimeValidationResult,
    RuntimeValidationRuleConfig,
    RuntimeValidationStatus,
    RuntimeValidationSummary,
    ValidationStatus,
)
from src.validation.structural_store import get_latest_structural_validation_for_deployment

logger = logging.getLogger(__name__)

_STRUCTURAL_PASS_STATUSES = {
    ValidationStatus.PASSED,
    ValidationStatus.PASSED_WITH_WARNINGS,
}


class RuntimeValidationError(Exception):
    """Raised when a runtime-equivalence validation cannot be run."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuntimeValidationService:
    """Compares source/target execution metrics; never touches structural
    validation state."""

    def validate(
        self,
        source_execution_id: int,
        target_execution_id: int,
        rules: Optional[RuntimeValidationRuleConfig] = None,
    ) -> RuntimeValidationResult:
        started = _now()
        rules = rules or RuntimeValidationRuleConfig()

        source_execution = get_execution(source_execution_id)
        if source_execution is None:
            raise RuntimeValidationError(
                f"Source execution {source_execution_id} not found.",
                "SOURCE_EXECUTION_NOT_FOUND",
            )
        if source_execution.side != ExecutionSide.SOURCE:
            raise RuntimeValidationError(
                f"Execution {source_execution_id} is not a source execution.",
                "NOT_A_SOURCE_EXECUTION",
            )

        target_execution = get_execution(target_execution_id)
        if target_execution is None:
            raise RuntimeValidationError(
                f"Target execution {target_execution_id} not found.",
                "TARGET_EXECUTION_NOT_FOUND",
            )
        if target_execution.side != ExecutionSide.TARGET:
            raise RuntimeValidationError(
                f"Execution {target_execution_id} is not a target execution.",
                "NOT_A_TARGET_EXECUTION",
            )

        plan_id = target_execution.plan_id or source_execution.plan_id
        if plan_id is None:
            raise RuntimeValidationError(
                "Neither execution references a migration plan.",
                "PLAN_REFERENCE_MISSING",
            )
        if (
            source_execution.plan_id is not None
            and target_execution.plan_id is not None
            and source_execution.plan_id != target_execution.plan_id
        ):
            raise RuntimeValidationError(
                "Source and target executions reference different plans.",
                "PLAN_MISMATCH",
            )

        plan_record = get_plan(plan_id)
        if plan_record is None:
            raise RuntimeValidationError(
                f"Plan {plan_id} not found.", "PLAN_NOT_FOUND"
            )
        plan = plan_record["plan"]
        deployment_id = target_execution.deployment_id or source_execution.deployment_id
        if deployment_id is None:
            raise RuntimeValidationError(
                "Neither execution references a deployment.",
                "DEPLOYMENT_REFERENCE_MISSING",
            )

        # ── Pre-flight authorization gate ───────────────────────────
        # Every check below must pass before ANY runtime-equivalence
        # comparison is produced. Each failure is specific and distinct;
        # nothing here ever invents/assumes a result.
        settings = get_settings()
        if not settings.runtime_execution_ready():
            raise RuntimeValidationError(
                "Runtime execution/validation is disabled (set "
                "RUNTIME_EXECUTION_ENABLED=true and configure ADF/Fabric).",
                "RUNTIME_EXECUTION_DISABLED",
            )

        deployment_record = get_deployment(deployment_id)
        if deployment_record is None:
            raise RuntimeValidationError(
                f"Deployment {deployment_id} not found.", "DEPLOYMENT_NOT_FOUND"
            )
        deployment = deployment_record["result"]
        if deployment.plan_id != plan_id:
            raise RuntimeValidationError(
                "Deployment does not belong to the given plan.",
                "PLAN_DEPLOYMENT_MISMATCH",
            )
        if deployment.mode != DeploymentMode.REAL:
            raise RuntimeValidationError(
                "Runtime validation requires a REAL deployment.",
                "DEPLOYMENT_NOT_REAL",
            )
        if deployment.status != DeploymentStatus.SUCCEEDED:
            raise RuntimeValidationError(
                "Runtime validation requires a SUCCEEDED REAL deployment.",
                "DEPLOYMENT_NOT_SUCCESSFUL",
            )

        current_fingerprint = compute_plan_package_fingerprint(plan)
        if deployment.plan_fingerprint and deployment.plan_fingerprint != current_fingerprint:
            raise RuntimeValidationError(
                "Package fingerprint has changed since deployment/approval.",
                "PACKAGE_FINGERPRINT_MISMATCH",
            )
        try:
            verify_plan_package(plan)
        except ArtifactPackageError as exc:
            raise RuntimeValidationError(
                f"Approved package verification failed: {exc}",
                "PACKAGE_VERIFICATION_FAILED",
            ) from exc

        # Read-only check: confirms a passing structural validation exists.
        # This NEVER writes to structural_validation_runs.
        structural = get_latest_structural_validation_for_deployment(deployment_id)
        if structural is None:
            raise RuntimeValidationError(
                "No structural validation has been run for this deployment.",
                "STRUCTURAL_VALIDATION_MISSING",
            )
        if structural.status not in _STRUCTURAL_PASS_STATUSES:
            raise RuntimeValidationError(
                "Structural validation for this deployment did not pass.",
                "STRUCTURAL_VALIDATION_NOT_PASSED",
            )

        checks: list[RuntimeValidationCheckResult] = []

        if (
            source_execution.status != ExecutionStatus.SUCCEEDED
            or target_execution.status != ExecutionStatus.SUCCEEDED
        ):
            checks.append(
                RuntimeValidationCheckResult(
                    name="execution_success",
                    status=RuntimeValidationStatus.FAIL,
                    source_value=source_execution.status.value,
                    target_value=target_execution.status.value,
                    explanation=(
                        "Source or target execution did not succeed "
                        f"(source={source_execution.status.value}, "
                        f"target={target_execution.status.value})."
                    ),
                )
            )
            overall = RuntimeValidationStatus.FAIL
        else:
            checks.extend(
                _compare_metrics(
                    source_execution.metrics, target_execution.metrics, rules
                )
            )
            overall = _aggregate(checks)

        summary = _summarize(checks)
        result = RuntimeValidationResult(
            discovery_snapshot_id=(
                plan_record["plan"].discovery_id
                or source_execution.discovery_snapshot_id
            ),
            plan_id=plan_id,
            plan_version=plan_record["version"],
            package_fingerprint=current_fingerprint,
            deployment_id=deployment_id,
            source_execution_id=source_execution_id,
            source_run_id=source_execution.run_id,
            target_execution_id=target_execution_id,
            target_run_id=target_execution.run_id,
            correlation_id=target_execution.correlation_id or source_execution.correlation_id,
            status=overall,
            started_at=started,
            completed_at=_now(),
            summary=summary,
            checks=checks,
        )
        return result


def _compare_metrics(
    source: Optional[RuntimeMetrics],
    target: Optional[RuntimeMetrics],
    rules: RuntimeValidationRuleConfig,
) -> list[RuntimeValidationCheckResult]:
    checks: list[RuntimeValidationCheckResult] = []

    def missing_check(name: str) -> RuntimeValidationCheckResult:
        return RuntimeValidationCheckResult(
            name=name,
            status=RuntimeValidationStatus.INCONCLUSIVE,
            source_value=None,
            target_value=None,
            explanation=(
                f"Metric '{name}' is missing from one or both executions; "
                "it cannot be assumed to match and is reported as "
                "inconclusive rather than a silent pass."
            ),
        )

    if source is None or target is None:
        checks.append(missing_check("all_metrics"))
        return checks

    # Row count
    if source.total_row_count is None or target.total_row_count is None:
        checks.append(missing_check("total_row_count"))
    else:
        diff = abs(source.total_row_count - target.total_row_count)
        ok = diff <= rules.row_count_tolerance
        checks.append(
            RuntimeValidationCheckResult(
                name="total_row_count",
                status=RuntimeValidationStatus.PASS if ok else RuntimeValidationStatus.FAIL,
                source_value=source.total_row_count,
                target_value=target.total_row_count,
                tolerance=rules.row_count_tolerance,
                explanation=(
                    "Row counts match within tolerance." if ok
                    else f"Row count differs by {diff} (tolerance {rules.row_count_tolerance})."
                ),
            )
        )

    # Schema
    if not source.schemas or not target.schemas:
        checks.append(missing_check("schemas"))
    else:
        ok = source.schemas == target.schemas
        checks.append(
            RuntimeValidationCheckResult(
                name="schema",
                status=RuntimeValidationStatus.PASS if ok else RuntimeValidationStatus.FAIL,
                source_value=source.schemas,
                target_value=target.schemas,
                explanation=(
                    "Schemas match." if ok
                    else "Column structure differs between source and target."
                ),
            )
        )

    # Numeric totals
    if not source.numeric_totals or not target.numeric_totals:
        checks.append(missing_check("numeric_totals"))
    else:
        for key in sorted(set(source.numeric_totals) | set(target.numeric_totals)):
            if key not in source.numeric_totals or key not in target.numeric_totals:
                checks.append(missing_check(f"numeric_total:{key}"))
                continue
            diff = abs(source.numeric_totals[key] - target.numeric_totals[key])
            ok = diff <= rules.numeric_total_tolerance
            checks.append(
                RuntimeValidationCheckResult(
                    name=f"numeric_total:{key}",
                    status=RuntimeValidationStatus.PASS if ok else RuntimeValidationStatus.FAIL,
                    source_value=source.numeric_totals[key],
                    target_value=target.numeric_totals[key],
                    tolerance=rules.numeric_total_tolerance,
                    explanation=(
                        f"Numeric total '{key}' matches within tolerance." if ok
                        else f"Numeric total '{key}' differs by {diff} "
                             f"(tolerance {rules.numeric_total_tolerance})."
                    ),
                )
            )

    # Grouped totals
    if not source.grouped_totals or not target.grouped_totals:
        checks.append(missing_check("grouped_totals"))
    else:
        for group in sorted(set(source.grouped_totals) | set(target.grouped_totals)):
            if group not in source.grouped_totals or group not in target.grouped_totals:
                checks.append(missing_check(f"grouped_total:{group}"))
                continue
            s_group, t_group = source.grouped_totals[group], target.grouped_totals[group]
            for key in sorted(set(s_group) | set(t_group)):
                if key not in s_group or key not in t_group:
                    checks.append(missing_check(f"grouped_total:{group}.{key}"))
                    continue
                diff = abs(s_group[key] - t_group[key])
                ok = diff <= rules.grouped_total_tolerance
                checks.append(
                    RuntimeValidationCheckResult(
                        name=f"grouped_total:{group}.{key}",
                        status=RuntimeValidationStatus.PASS if ok else RuntimeValidationStatus.FAIL,
                        source_value=s_group[key],
                        target_value=t_group[key],
                        tolerance=rules.grouped_total_tolerance,
                        explanation=(
                            f"Grouped total '{group}.{key}' matches within tolerance." if ok
                            else f"Grouped total '{group}.{key}' differs by {diff} "
                                 f"(tolerance {rules.grouped_total_tolerance})."
                        ),
                    )
                )

    # Duration (never blocking on its own; at most a warning)
    if source.duration_seconds is None or target.duration_seconds is None:
        checks.append(missing_check("duration_seconds"))
    else:
        base = source.duration_seconds or 0.0
        diff_pct = 0.0 if base == 0 else abs(source.duration_seconds - target.duration_seconds) / base
        ok = diff_pct <= rules.duration_tolerance_pct
        status = (
            RuntimeValidationStatus.PASS if ok
            else RuntimeValidationStatus.PASS_WITH_WARNINGS if rules.allow_duration_warning
            else RuntimeValidationStatus.FAIL
        )
        checks.append(
            RuntimeValidationCheckResult(
                name="duration_seconds",
                status=status,
                source_value=source.duration_seconds,
                target_value=target.duration_seconds,
                tolerance=rules.duration_tolerance_pct,
                explanation=(
                    "Duration is within tolerance." if ok
                    else f"Duration differs by {diff_pct * 100:.1f}% "
                         f"(tolerance {rules.duration_tolerance_pct * 100:.0f}%); "
                         "duration-only deviations do not fail the run."
                ),
            )
        )

    return checks


def _aggregate(checks: list[RuntimeValidationCheckResult]) -> RuntimeValidationStatus:
    statuses = {check.status for check in checks}
    if RuntimeValidationStatus.FAIL in statuses:
        return RuntimeValidationStatus.FAIL
    if RuntimeValidationStatus.INCONCLUSIVE in statuses:
        return RuntimeValidationStatus.INCONCLUSIVE
    if RuntimeValidationStatus.PASS_WITH_WARNINGS in statuses:
        return RuntimeValidationStatus.PASS_WITH_WARNINGS
    return RuntimeValidationStatus.PASS


def _summarize(checks: list[RuntimeValidationCheckResult]) -> RuntimeValidationSummary:
    summary = RuntimeValidationSummary(total_checks=len(checks))
    for check in checks:
        if check.status == RuntimeValidationStatus.PASS:
            summary.passed += 1
        elif check.status == RuntimeValidationStatus.PASS_WITH_WARNINGS:
            summary.warnings += 1
        elif check.status == RuntimeValidationStatus.FAIL:
            summary.failed += 1
        elif check.status == RuntimeValidationStatus.INCONCLUSIVE:
            summary.inconclusive += 1
    return summary
