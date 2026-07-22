"""Validation Engine — Phase 8."""

from datetime import datetime, timezone
import math
import logging

from src.models.schemas import (
    CheckStatus,
    DatasetMetrics,
    ValidationCheckResult,
    ValidationResult,
    ValidationStatus,
    ValidationSummary,
    ValidationRule
)
from src.migration.deployment_store import get_deployment
from src.migration.plan_store import get_plan
from src.models.schemas import DeploymentStatus

logger = logging.getLogger(__name__)

class ValidationError(Exception):
    pass

class ValidationService:
    def validate(
        self,
        deployment_id: int,
        source_metrics: dict[str, DatasetMetrics],
        target_metrics: dict[str, DatasetMetrics]
    ) -> ValidationResult:
        
        started_at = datetime.now(timezone.utc).isoformat()
        
        # 1. Fetch deployment
        deployment = get_deployment(deployment_id)
        if not deployment:
            raise ValidationError(f"Deployment {deployment_id} not found.")
        
        if deployment["status"] != DeploymentStatus.SUCCEEDED.value:
            raise ValidationError(f"Deployment {deployment_id} must be SUCCEEDED to validate (is {deployment['status']}).")
        
        if deployment["mode"] != "MOCK":
            raise ValidationError(f"Deployment {deployment_id} must be MOCK to validate (is {deployment['mode']}).")

        # 2. Fetch plan and rules
        plan_record = get_plan(deployment["plan_id"])
        if not plan_record:
            raise ValidationError(f"Plan {deployment['plan_id']} not found.")
        
        rules = plan_record["plan"].validation_rules
        checks: list[ValidationCheckResult] = []
        
        # 3. Evaluate rules
        overall_status = ValidationStatus.PASSED
        summary = ValidationSummary(total_checks=len(rules))
        
        for rule in rules:
            check_res = self._evaluate_rule(rule, source_metrics, target_metrics)
            checks.append(check_res)
            
            if check_res.status == CheckStatus.PASSED:
                summary.passed += 1
            elif check_res.status == CheckStatus.WARNING:
                summary.warnings += 1
                if overall_status == ValidationStatus.PASSED:
                    overall_status = ValidationStatus.PASSED_WITH_WARNINGS
            elif check_res.status == CheckStatus.FAILED:
                summary.failed += 1
                if rule.blocking:
                    overall_status = ValidationStatus.FAILED
            elif check_res.status == CheckStatus.SKIPPED:
                summary.skipped += 1
                
        completed_at = datetime.now(timezone.utc).isoformat()
        
        return ValidationResult(
            deployment_id=deployment_id,
            plan_id=deployment["plan_id"],
            status=overall_status,
            started_at=started_at,
            completed_at=completed_at,
            summary=summary,
            source_metrics=source_metrics,
            target_metrics=target_metrics,
            checks=checks
        )

    def _evaluate_rule(
        self, rule: ValidationRule, src_metrics: dict[str, DatasetMetrics], tgt_metrics: dict[str, DatasetMetrics]
    ) -> ValidationCheckResult:
        
        def get_val(metrics: dict[str, DatasetMetrics], ref: str, rtype: str):
            if rtype == "run_status": return metrics.get("pipeline", DatasetMetrics()).run_status
            if rtype == "runtime": return metrics.get("pipeline", DatasetMetrics()).runtime_seconds
            
            if "enriched_orders" in ref:
                m = metrics.get("enriched_orders", DatasetMetrics())
                if rtype == "row_count": return m.row_count
                if rtype == "schema": return m.schema_hash
                if "GrossAmount" in ref: return m.gross_total
                if "DiscountAmount" in ref: return m.discount_total
                if "NetAmount" in ref: return m.net_total
                if rtype == "sum": return m.gross_total # Fallback
            if "rejected_orders" in ref and rtype == "row_count":
                return metrics.get("rejected_orders", DatasetMetrics()).row_count
            if "customer_summary" in ref:
                m = metrics.get("customer_summary", DatasetMetrics())
                if rtype == "row_count": return m.row_count
                if rtype == "grouped_sum": return m.customer_region_totals
            return None

        s_val = get_val(src_metrics, rule.source, rule.rule_type)
        t_val = get_val(tgt_metrics, rule.target, rule.rule_type)
        
        if s_val is None or t_val is None:
            return ValidationCheckResult(
                rule_name=rule.name,
                rule_type=rule.rule_type,
                status=CheckStatus.FAILED if rule.blocking else CheckStatus.WARNING,
                source_value=s_val,
                target_value=t_val,
                message="Missing metrics"
            )

        status = CheckStatus.PASSED
        msg = "Exact match"
        
        if rule.comparison == "equals":
            if s_val != t_val:
                status = CheckStatus.FAILED if rule.blocking else CheckStatus.WARNING
                msg = f"Mismatch: {s_val} != {t_val}"
        
        elif rule.comparison == "abs_diff_within_tolerance":
            if isinstance(s_val, dict) and isinstance(t_val, dict):
                # Dict comparison
                for k in s_val.keys():
                    if k not in t_val:
                        status = CheckStatus.FAILED if rule.blocking else CheckStatus.WARNING
                        msg = f"Missing key in target: {k}"
                        break
                    diff = abs(s_val[k] - t_val[k])
                    if diff > rule.tolerance:
                        status = CheckStatus.FAILED if rule.blocking else CheckStatus.WARNING
                        msg = f"Key {k} difference {diff} > {rule.tolerance}"
                        break
            else:
                diff = abs(s_val - t_val)
                if diff > rule.tolerance:
                    status = CheckStatus.FAILED if rule.blocking else CheckStatus.WARNING
                    msg = f"Difference {diff} > {rule.tolerance}"
                else:
                    msg = "Within tolerance"
                    
        elif rule.comparison == "within_tolerance":
            # Percentage difference (e.g., runtime)
            if s_val == 0:
                diff_pct = 0 if t_val == 0 else 1.0
            else:
                diff_pct = abs(s_val - t_val) / s_val
            
            if diff_pct > rule.tolerance:
                status = CheckStatus.FAILED if rule.blocking else CheckStatus.WARNING
                msg = f"Difference {diff_pct*100:.1f}% > {rule.tolerance*100}%"
            else:
                msg = "Within tolerance"

        return ValidationCheckResult(
            rule_name=rule.name,
            rule_type=rule.rule_type,
            status=status,
            source_value=s_val,
            target_value=t_val,
            message=msg
        )
