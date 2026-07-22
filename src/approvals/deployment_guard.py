"""Deployment authorization guard — Phase 6.

The single gate that decides whether a plan may be deployed. It never
deploys anything: it validates that an approval genuinely authorizes a
specific, unchanged, executable plan and raises a clear error otherwise.
"""

import logging
import re
from dataclasses import dataclass, field

from src.artifacts import ArtifactPackageError, canonical_json
from src.approvals.approval_store import get_approval, update_status
from src.migration.plan_store import (
    compute_plan_package_fingerprint,
    get_plan,
    verify_plan_package,
)
from src.models.schemas import ApprovalStatus

logger = logging.getLogger(__name__)

# Action-content keywords that would indicate a destructive step.
_DESTRUCTIVE_KEYWORDS = ("delete", "drop", "truncate")


class DeploymentAuthorizationError(Exception):
    """Raised when a deployment is not authorized."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class AuthorizationResult:
    """Result of a successful authorization check."""

    authorized: bool
    plan_id: int
    approval_id: int
    plan_version: int
    plan_fingerprint: str
    package_id: str
    artifact_count: int
    checks_passed: list[str] = field(default_factory=list)


def _has_destructive_action(plan) -> bool:
    """True if any action looks like a delete/drop/truncate."""
    for action in plan.actions:
        blob = (
            f"{action.action_type} {action.reason} {action.target_item_name}"
        ).lower()
        if any(word in blob for word in _DESTRUCTIVE_KEYWORDS):
            return True
    if plan.generated_package is not None:
        blob = canonical_json(
            plan.generated_package.model_dump(mode="json")
        ).lower()
        if any(
            re.search(rf"\b{re.escape(word)}\b", blob)
            for word in _DESTRUCTIVE_KEYWORDS
        ):
            return True
    return False


def validate_deployment_authorization(
    plan_id: int, approval_id: int
) -> AuthorizationResult:
    """Validate that `approval_id` authorizes deploying `plan_id`.

    Raises DeploymentAuthorizationError with a specific code on the first
    failed check. Returns an AuthorizationResult on success. Performs no
    deployment.
    """
    checks: list[str] = []

    record = get_plan(plan_id)
    if record is None:
        raise DeploymentAuthorizationError(
            f"Plan {plan_id} does not exist.", "PLAN_NOT_FOUND"
        )
    checks.append("plan_exists")

    approval = get_approval(approval_id)
    if approval is None:
        raise DeploymentAuthorizationError(
            f"Approval {approval_id} does not exist.", "APPROVAL_NOT_FOUND"
        )
    checks.append("approval_exists")

    if approval.plan_id != plan_id:
        raise DeploymentAuthorizationError(
            f"Approval {approval_id} is bound to plan {approval.plan_id}, "
            f"not {plan_id}.",
            "PLAN_ID_MISMATCH",
        )
    checks.append("plan_id_matches")

    if ApprovalStatus(approval.status) == ApprovalStatus.INVALIDATED:
        raise DeploymentAuthorizationError(
            "Approval has been invalidated by a plan change.",
            "APPROVAL_INVALIDATED",
        )
    checks.append("approval_not_invalidated")

    if ApprovalStatus(approval.status) != ApprovalStatus.APPROVED:
        raise DeploymentAuthorizationError(
            f"Approval status is {approval.status}, not APPROVED.",
            "NOT_APPROVED",
        )
    checks.append("approval_approved")

    if approval.plan_version != record["version"]:
        raise DeploymentAuthorizationError(
            f"Approval version {approval.plan_version} does not match plan "
            f"version {record['version']}.",
            "VERSION_MISMATCH",
        )
    checks.append("version_matches")

    current_fingerprint = compute_plan_package_fingerprint(record["plan"])
    if approval.plan_fingerprint != current_fingerprint:
        update_status(
            approval_id,
            ApprovalStatus.INVALIDATED,
            decided_by="system",
            decision_comment="Approved plan/package fingerprint changed.",
        )
        raise DeploymentAuthorizationError(
            "Plan fingerprint has changed since approval.",
            "FINGERPRINT_MISMATCH",
        )
    checks.append("fingerprint_matches")

    try:
        verified_package = verify_plan_package(record["plan"])
    except ArtifactPackageError as exc:
        update_status(
            approval_id,
            ApprovalStatus.INVALIDATED,
            decided_by="system",
            decision_comment="Approved generated package is missing or modified.",
        )
        message = str(exc)
        code = (
            "PACKAGE_MISSING"
            if "No such file" in message or "missing package files" in message
            else "PACKAGE_INVALID"
        )
        raise DeploymentAuthorizationError(message, code) from exc
    checks.append("package_exists")
    checks.append("manifest_matches")
    checks.append("artifact_digests_match")
    checks.append("no_unexpected_files")

    if not record["plan"].executable:
        raise DeploymentAuthorizationError(
            "Plan is not executable.", "PLAN_NOT_EXECUTABLE"
        )
    checks.append("plan_executable")

    if _has_destructive_action(record["plan"]):
        raise DeploymentAuthorizationError(
            "Plan contains a destructive (delete/drop) action.",
            "DELETE_ACTION_PRESENT",
        )
    checks.append("no_delete_action")

    logger.info(
        "Deployment authorized: plan=%d approval=%d.", plan_id, approval_id
    )
    return AuthorizationResult(
        authorized=True,
        plan_id=plan_id,
        approval_id=approval_id,
        plan_version=record["version"],
        plan_fingerprint=current_fingerprint,
        package_id=verified_package.package_id,
        artifact_count=len(verified_package.artifacts),
        checks_passed=checks,
    )
