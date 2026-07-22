"""Safe artifact-definition migration reports."""

import html
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.approvals.approval_store import get_approval
from src.config import get_settings
from src.migration.deployment_store import get_deployment
from src.migration.discovery_store import get_discovery
from src.migration.assessment_store import get_assessment
from src.migration.plan_store import get_plan
from src.models.schemas import MigrationReport
from src.validation.runtime_execution_validation_store import (
    get_latest_runtime_execution_validation,
)
from src.validation.runtime_store import get_latest_runtime_validation
from src.validation.structural_store import get_structural_validation

REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "reports"
_SENSITIVE = re.compile(
    r"password|secret|token|authorization|api[_-]?key|accountkey|"
    r"connectionstring|serviceprincipalkey|clientsecret",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?i)(password|secret|token|accountkey|connectionstring)\s*=\s*[^;\s]+"
)


def get_reports_dir() -> Path:
    configured = get_settings().reports_dir
    return Path(configured).resolve() if configured else REPORTS_DIR.resolve()


def report_path(validation_id: int, extension: str, reports_dir: Path | None = None) -> Path:
    if not isinstance(validation_id, int) or validation_id < 1:
        raise ValueError("validation_id must be a positive integer")
    if extension not in {"json", "html"}:
        raise ValueError("unsupported report extension")
    root = (Path(reports_dir).resolve() if reports_dir else get_reports_dir())
    candidate = (root / f"{validation_id}.{extension}").resolve()
    if candidate.parent != root:
        raise ValueError("report path escapes configured directory")
    return candidate


def redact_secrets(value: Any) -> Any:
    """Recursively redact sensitive keys and credential-like string values."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            str(key): ("***REDACTED***" if _SENSITIVE.search(str(key))
                       else redact_secrets(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        value = _SECRET_VALUE.sub(lambda match: f"{match.group(1)}=***REDACTED***", value)
        if value.lower().startswith("bearer "):
            return "Bearer ***REDACTED***"
    return value


def generate_report(validation_id: int, reports_dir: Path | None = None) -> MigrationReport:
    validation = get_structural_validation(validation_id)
    if not validation:
        raise ValueError(f"Validation {validation_id} not found.")
    deployment_record = get_deployment(validation.deployment_id)
    plan_record = get_plan(validation.plan_id)
    if not deployment_record or not plan_record or not plan_record["plan"].generated_package:
        raise ValueError("Validation dependencies are missing.")
    discovery_record = get_discovery(validation.discovery_id)
    if not discovery_record:
        raise ValueError("Source discovery snapshot is missing.")
    approval = get_approval(validation.approval_id)
    assessment_record = (
        get_assessment(plan_record["assessment_id"])
        if plan_record.get("assessment_id") else None
    )
    plan = plan_record["plan"]
    artifacts = plan.generated_package.artifacts

    conversions = [
        {"artifact_id": artifact.artifact_id, **note.model_dump(mode="json")}
        for artifact in artifacts for note in artifact.conversion_notes
    ]
    unsupported = [
        {"artifact_id": artifact.artifact_id, "property": prop}
        for artifact in artifacts for prop in artifact.unsupported_properties
    ]
    manual = [item.model_dump(mode="json") for item in plan.manual_actions]
    manual.extend(
        {"artifact_id": artifact.artifact_id, "action": action}
        for artifact in artifacts for action in artifact.manual_actions
    )
    runtime = get_latest_runtime_validation()
    if runtime and runtime.deployment_id != validation.deployment_id:
        runtime = None

    # Optional Phase 11 runtime-equivalence (execution-linked) appendix.
    # Only included when a runtime validation exists for this plan. Matched
    # by plan id, not deployment id: structural validation always runs
    # against a MOCK deployment while a Phase 11 runtime validation is
    # linked to a separate REAL deployment of the same plan.
    runtime_execution_validation = get_latest_runtime_execution_validation(
        plan_id=validation.plan_id
    )

    report = MigrationReport(
        report_id=f"structural-{validation_id}",
        generated_at=datetime.now(timezone.utc).isoformat(),
        workflow_stages=redact_secrets({
            "discover": {
                "discovery_id": discovery_record["id"],
                "summary": discovery_record["result"].summary,
            },
            "assess": {
                "assessment_id": plan_record.get("assessment_id"),
                "overall_status": (
                    assessment_record["overall_status"] if assessment_record else None
                ),
                "summary": (
                    assessment_record["result"].summary if assessment_record else None
                ),
            },
            "plan": {
                "plan_id": validation.plan_id,
                "version": plan_record["version"],
                "package_id": plan.generated_package.package_id,
                "package_fingerprint": validation.package_fingerprint,
                "mapping_count": len(plan.mappings),
            },
            "approve": {
                "approval_id": validation.approval_id,
                "status": approval.status.value if approval else None,
                "package_fingerprint": approval.plan_fingerprint if approval else None,
            },
            "deploy": {
                "deployment_id": validation.deployment_id,
                "mode": deployment_record["mode"],
                "status": deployment_record["status"],
                "artifact_result_count": len(deployment_record["result"].steps),
            },
            "validate": {
                "validation_id": validation.validation_id,
                "status": validation.status.value,
                "summary": validation.summary,
            },
        }),
        source_artifacts=redact_secrets([
            asset.model_dump(mode="json") for asset in discovery_record["result"].assets
            if not asset.is_component
        ]),
        generated_artifacts=redact_secrets([a.model_dump(mode="json") for a in artifacts]),
        mappings=redact_secrets([m.model_dump(mode="json") for m in plan.mappings]),
        property_conversions=redact_secrets(conversions),
        unsupported_properties=redact_secrets(unsupported),
        manual_actions=redact_secrets(manual),
        approval=redact_secrets(approval),
        deployment=redact_secrets(deployment_record["result"]),
        structural_validation=validation,
        runtime_validation=redact_secrets(runtime),
        runtime_execution_validation=redact_secrets(runtime_execution_validation),
    )
    root = Path(reports_dir).resolve() if reports_dir else get_reports_dir()
    root.mkdir(parents=True, exist_ok=True)
    _atomic_write(report_path(validation_id, "json", root), report.model_dump_json(indent=2))
    _atomic_write(report_path(validation_id, "html", root), _render_html(report))
    return report


def _atomic_write(path: Path, content: str) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{_e(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(item)}</td>" for item in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _flatten(value: Any, prefix: str = "$") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        rows = []
        for key in sorted(value):
            rows.extend(_flatten(value[key], f"{prefix}.{key}"))
        return rows
    if isinstance(value, list):
        rows = []
        for index, item in enumerate(value):
            rows.extend(_flatten(item, f"{prefix}[{index}]"))
        return rows
    return [(prefix, "" if value is None else str(value))]


def _render_html(report: MigrationReport) -> str:
    validation = report.structural_validation
    source_rows = [[a.get("source_reference"), a.get("asset_type"), a.get("asset_name")]
                   for a in report.source_artifacts]
    generated_rows = [[a.get("artifact_id"), a.get("target_type"), a.get("target_name"),
                       a.get("content_digest")] for a in report.generated_artifacts]
    check_rows = [[c.category, c.status.value, c.source_reference, c.target_artifact_id, c.message]
                  for c in validation.checks]
    conversion_rows = [[c.get("artifact_id"), c.get("source_path"), c.get("target_path"),
                        c.get("disposition"), c.get("note")] for c in report.property_conversions]
    mapping_rows = [[m.get("source_type"), m.get("source_asset"),
                     m.get("target_item_type"), m.get("target_item_name"),
                     m.get("assessment_status")] for m in report.mappings]
    warning_rows = [[artifact.get("artifact_id"), warning]
                    for artifact in report.generated_artifacts
                    for warning in artifact.get("warnings", [])]
    deployment_rows = [[step.get("artifact_id"), step.get("target_item_type"),
                        step.get("target_item_name"), step.get("status"),
                        step.get("content_digest"), step.get("message")]
                       for step in (report.deployment or {}).get("steps", [])]
    definition_sections = []
    for artifact in report.generated_artifacts:
        definition_sections.append(
            f"<h3>{_e(artifact.get('artifact_id'))}</h3>" +
            _table(["Definition path", "Value"], _flatten(artifact.get("generated_definition", {})))
        )
    approval_fp = (report.approval or {}).get("plan_fingerprint", "")
    approval_rows = [[key, value] for key, value in (report.approval or {}).items()
                     if key in {"status", "requested_by", "decided_by", "request_comment", "decision_comment", "plan_fingerprint"}]
    stage_rows = [[name, stage.get("status") or stage.get("overall_status") or "captured"]
                  for name, stage in report.workflow_stages.items()]
    runtime_appendix = ""
    rev = report.runtime_execution_validation
    if rev:
        runtime_check_rows = [
            [c.get("name"), c.get("status"), c.get("source_value"), c.get("target_value"),
             c.get("tolerance"), c.get("explanation")]
            for c in rev.get("checks", [])
        ]
        summary = rev.get("summary", {}) or {}
        runtime_appendix = "".join([
            "<h2>Optional runtime-equivalence validation (appendix)</h2>",
            "<p><strong>This section never alters the structural validation status above.</strong> "
            "It compares safe runtime metrics from a real, controlled source (ADF) execution and a "
            "real, controlled target (Fabric) execution of the same migrated pipeline.</p>",
            f"<p>Runtime status: {_e(rev.get('status'))} — "
            f"{summary.get('passed', 0)} passed, {summary.get('warnings', 0)} warnings, "
            f"{summary.get('failed', 0)} failed, {summary.get('inconclusive', 0)} inconclusive "
            f"({summary.get('total_checks', 0)} checks).</p>",
            f"<p>Source execution: {_e(rev.get('source_execution_id'))} (run {_e(rev.get('source_run_id'))}) — "
            f"Target execution: {_e(rev.get('target_execution_id'))} (run {_e(rev.get('target_run_id'))}) — "
            f"Correlation: <code>{_e(rev.get('correlation_id'))}</code></p>",
            _table(["Check", "Status", "Source", "Target", "Tolerance", "Explanation"], runtime_check_rows),
        ])
    return "".join([
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Artifact Migration Report</title>",
        "<style>body{font-family:Arial,sans-serif;margin:2rem;color:#182230}table{border-collapse:collapse;width:100%;margin-bottom:1.5rem}th,td{border:1px solid #ccd5df;padding:.45rem;text-align:left;vertical-align:top}th{background:#eef3f8}code{overflow-wrap:anywhere}</style></head><body>",
        f"<h1>Artifact-definition migration report</h1><p>Status: {_e(validation.status.value)}</p>",
        "<h2>Workflow stages</h2>", _table(["Stage", "Result"], stage_rows),
        f"<p>Approval package fingerprint: <code>{_e(approval_fp)}</code></p>",
        "<h2>Approval</h2>", _table(["Field", "Value"], approval_rows),
        "<h2>Source artifacts</h2>", _table(["Source reference", "Type", "Name"], source_rows),
        "<h2>Generated Fabric artifacts</h2>", _table(["Artifact ID", "Target type", "Name", "Digest"], generated_rows),
        "<h2>Source-to-target mappings</h2>", _table(["Source type", "Source", "Target type", "Target", "Assessment"], mapping_rows),
        "<h2>Generated definitions</h2>", "".join(definition_sections),
        "<h2>Property conversions</h2>", _table(["Artifact", "Source", "Target", "Disposition", "Note"], conversion_rows),
        "<h2>Warnings</h2>", _table(["Artifact", "Warning"], warning_rows),
        "<h2>Mock deployment results</h2>", _table(["Artifact", "Target type", "Name", "Status", "Digest", "Message"], deployment_rows),
        "<h2>Structural validation</h2>", _table(["Category", "Status", "Source", "Target", "Result"], check_rows),
        "<h2>Unsupported properties</h2>", _table(["Artifact", "Property"], [[i.get("artifact_id"), i.get("property")] for i in report.unsupported_properties]),
        "<h2>Manual actions</h2>", _table(["Artifact/source", "Action"], [[i.get("artifact_id") or i.get("source_asset"), i.get("action") or i.get("recommended_action")] for i in report.manual_actions]),
        runtime_appendix,
        "</body></html>",
    ])
