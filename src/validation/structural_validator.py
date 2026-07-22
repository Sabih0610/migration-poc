"""Artifact-definition structural validation (no customer data checks)."""

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from src.approvals.approval_store import get_approval
from src.artifacts import ArtifactPackageError, validate_generated_artifact
from src.migration.deployment_store import get_deployment
from src.migration.discovery_store import get_discovery, get_latest_discovery
from src.migration.plan_store import (
    compute_plan_package_fingerprint,
    get_plan,
    verify_plan_package,
)
from src.models.schemas import (
    ApprovalStatus,
    CheckStatus,
    ConversionDisposition,
    DeploymentMode,
    DeploymentStatus,
    DeployableTargetType,
    StructuralValidationCheck,
    StructuralValidationResult,
    StructuralValidationSummary,
    ValidationStatus,
)


class StructuralValidationError(RuntimeError):
    """The requested deployment is not eligible for structural validation."""


class StructuralValidationService:
    """Compare the persisted source, package, approval, and mock deployment."""

    def validate(self, deployment_id: int) -> StructuralValidationResult:
        started = _now()
        deployment_record = get_deployment(deployment_id)
        if not deployment_record:
            raise StructuralValidationError(f"Deployment {deployment_id} not found.")
        deployment = deployment_record["result"]
        if deployment.mode != DeploymentMode.MOCK:
            raise StructuralValidationError(
                "Structural validation requires a MOCK deployment."
            )
        if deployment.status != DeploymentStatus.SUCCEEDED:
            raise StructuralValidationError(
                "Structural validation requires a successful deployment."
            )

        plan_record = get_plan(deployment.plan_id)
        if not plan_record:
            raise StructuralValidationError("The deployed plan no longer exists.")
        plan = plan_record["plan"]
        if not plan.generated_package:
            raise StructuralValidationError("The deployed plan has no package.")

        approval = get_approval(deployment.approval_id)
        if (
            not approval
            or approval.status != ApprovalStatus.APPROVED
            or approval.plan_id != deployment.plan_id
            or approval.approval_id != deployment.approval_id
        ):
            raise StructuralValidationError(
                "Structural validation requires the deployment's approved package."
            )

        discovery_record = (
            get_discovery(plan.discovery_id)
            if plan.discovery_id is not None
            else get_latest_discovery()
        )
        if not discovery_record:
            raise StructuralValidationError("The persisted source snapshot is missing.")
        discovery = discovery_record["result"]
        package = plan.generated_package
        artifacts = package.artifacts
        by_id = {artifact.artifact_id: artifact for artifact in artifacts}
        deployed = {step.artifact_id: step for step in deployment.steps if step.artifact_id}
        checks: list[StructuralValidationCheck] = []

        def add(category, ok, message, *, warning=False, source=None, target=None, details=None):
            checks.append(StructuralValidationCheck(
                category=category,
                status=(CheckStatus.PASSED if ok else
                        CheckStatus.WARNING if warning else CheckStatus.FAILED),
                message=message,
                source_reference=source,
                target_artifact_id=target,
                details=details or {},
            ))

        source_assets = [asset for asset in discovery.assets if not asset.is_component]
        mapped = {(mapping.source_type, mapping.source_asset) for mapping in plan.mappings}
        missing_mappings = [
            asset.source_reference or f"{asset.asset_type}:{asset.asset_name}"
            for asset in source_assets
            if (asset.asset_type, asset.asset_name) not in mapped
        ]
        add("source_to_target_mapping_coverage", not missing_mappings,
            "Every source artifact has a target mapping." if not missing_mappings
            else "Source artifacts are missing target mappings.",
            details={"missing": missing_mappings, "source_count": len(source_assets)})

        source_activities = [c for c in discovery.components if c.component_type == "activity"]
        generated_activity_names = set()
        deployed_activity_names = set()
        for artifact in artifacts:
            if artifact.target_type == DeployableTargetType.DATA_PIPELINE:
                generated_activity_names.update(_activity_names(
                    artifact.generated_definition.get("properties", {}).get("activities", [])
                ))
                deployed_step = deployed.get(artifact.artifact_id)
                deployed_activity_names.update(_activity_names(
                    (deployed_step.generated_definition or {}).get("properties", {}).get("activities", [])
                    if deployed_step else []
                ))
        expected_activity_names = {c.component_name for c in source_activities}
        missing_package_activities = sorted(expected_activity_names - generated_activity_names)
        missing_deployed_activities = sorted(expected_activity_names - deployed_activity_names)
        missing_activities = missing_package_activities or missing_deployed_activities
        add("activity_coverage", not missing_activities,
            "All nested pipeline activities are represented in package and deployment." if not missing_activities
            else "Nested activities are missing from generated pipelines.",
            details={"missing_in_package": missing_package_activities,
                     "missing_in_deployment": missing_deployed_activities,
                     "source_count": len(source_activities)})

        source_transforms = [c for c in discovery.components if c.component_type == "transformation"]
        transform_failures = []
        for parent in sorted({c.parent_reference for c in source_transforms}):
            expected = [c.component_name for c in sorted(
                (c for c in source_transforms if c.parent_reference == parent),
                key=lambda item: item.order,
            )]
            candidate = next((a for a in artifacts if a.source_reference == parent and
                              a.target_type == DeployableTargetType.DATAFLOW_GEN2), None)
            actual = [item.get("name") for item in (
                candidate.generated_definition.get("properties", {}).get("transformations", [])
                if candidate else [])]
            deployed_step = deployed.get(candidate.artifact_id) if candidate else None
            deployed_actual = [item.get("name") for item in (
                (deployed_step.generated_definition or {}).get("properties", {}).get("transformations", [])
                if deployed_step else [])]
            if actual != expected or deployed_actual != expected:
                transform_failures.append({"source": parent, "expected": expected,
                                           "package": actual, "deployment": deployed_actual})
        add("transformation_coverage_and_order", not transform_failures,
            "All transformations are preserved in source order." if not transform_failures
            else "Transformation membership or order differs.", details={"failures": transform_failures})

        pipeline_failures = {"parameters": [], "variables": []}
        for pipeline in discovery.inventory.pipelines:
            artifact = next((a for a in artifacts if a.source_reference == f"pipeline:{pipeline.name}"), None)
            properties = artifact.generated_definition.get("properties", {}) if artifact else {}
            if properties.get("parameters", {}) != (pipeline.properties.parameters or {}):
                pipeline_failures["parameters"].append(pipeline.name)
            if properties.get("variables", {}) != (pipeline.properties.variables or {}):
                pipeline_failures["variables"].append(pipeline.name)
        add("parameter_preservation", not pipeline_failures["parameters"],
            "Pipeline parameters are preserved." if not pipeline_failures["parameters"]
            else "Pipeline parameters differ.", details={"pipelines": pipeline_failures["parameters"]})
        add("variable_preservation", not pipeline_failures["variables"],
            "Pipeline variables are preserved." if not pipeline_failures["variables"]
            else "Pipeline variables differ.", details={"pipelines": pipeline_failures["variables"]})

        generated_values = [a.generated_definition for a in artifacts]
        missing_expressions = [
            {"source": expression.source_reference, "path": expression.property_path}
            for expression in discovery.expressions
            if not any(_contains_value(definition, expression.value) for definition in generated_values)
        ]
        add("expression_conversion_or_preservation", not missing_expressions,
            "All discovered expressions are preserved or converted." if not missing_expressions
            else "Some source expressions cannot be traced in target definitions.",
            details={"missing": missing_expressions})

        name_artifacts = _artifacts_by_source_name(artifacts)
        dependency_failures = []
        for edge in discovery.dependencies:
            dependent = name_artifacts.get(edge.source)
            dependency = name_artifacts.get(edge.target)
            if not dependent or not dependency or dependent.artifact_id == dependency.artifact_id:
                continue
            declared = set(dependent.dependencies + dependent.connection_references)
            if dependency.artifact_id not in declared:
                dependency_failures.append({"dependent": edge.source, "dependency": edge.target})
        step_positions = {step.artifact_id: index for index, step in enumerate(deployment.steps) if step.artifact_id}
        order_failures = [
            {"artifact": artifact.artifact_id, "dependency": dependency}
            for artifact in artifacts for dependency in artifact.dependencies
            if dependency in by_id and (
                dependency not in step_positions or artifact.artifact_id not in step_positions
                or step_positions[dependency] >= step_positions[artifact.artifact_id]
            )
        ]
        add("dependency_and_execution_order_preservation",
            not dependency_failures and not order_failures,
            "Dependencies are declared and deployed before dependents."
            if not dependency_failures and not order_failures
            else "Dependency declarations or deployment order differ.",
            details={"dependency_failures": dependency_failures, "order_failures": order_failures})

        trigger_missing = [
            trigger.name for trigger in discovery.inventory.triggers
            if not any(a.source_reference == f"trigger:{trigger.name}" and
                       a.target_type == DeployableTargetType.SCHEDULE for a in artifacts)
        ]
        add("trigger_to_schedule_mapping", not trigger_missing,
            "Every trigger maps to a Fabric schedule." if not trigger_missing
            else "Triggers are missing generated schedules.", details={"missing": trigger_missing})

        connection_failures = []
        for reference in discovery.connection_references:
            source_artifact = next((a for a in artifacts if a.source_reference == reference.source_reference), None)
            connection = next((a for a in artifacts if a.target_type == DeployableTargetType.CONNECTION
                               and a.target_name == reference.connection_name), None)
            if source_artifact and connection and connection.artifact_id not in (
                source_artifact.connection_references + source_artifact.dependencies
            ):
                connection_failures.append(reference.model_dump(mode="json"))
        add("connection_reference_mapping", not connection_failures,
            "Connection references resolve to generated connections." if not connection_failures
            else "Connection references are not preserved.", details={"failures": connection_failures})

        unsupported_errors = []
        manual_errors = []
        for artifact in artifacts:
            dispositions = {note.disposition for note in artifact.conversion_notes}
            if ConversionDisposition.UNSUPPORTED in dispositions and not artifact.unsupported_properties:
                unsupported_errors.append(artifact.artifact_id)
            if ConversionDisposition.MANUAL in dispositions and not artifact.manual_actions:
                manual_errors.append(artifact.artifact_id)
        add("unsupported_property_reporting", not unsupported_errors,
            "Unsupported properties are explicitly reported." if not unsupported_errors
            else "Unsupported conversions lack reporting.", details={"artifacts": unsupported_errors})
        add("manual_action_reporting", not manual_errors,
            "Manual conversions are explicitly reported." if not manual_errors
            else "Manual conversions lack actions.", details={"artifacts": manual_errors})

        schema_errors = {}
        for artifact in artifacts:
            schema = validate_generated_artifact(artifact)
            if not schema.valid:
                schema_errors[artifact.artifact_id] = schema.errors
        add("generated_definition_schema_validity", not schema_errors,
            "All generated definitions satisfy internal schemas." if not schema_errors
            else "Generated definitions fail schema checks.", details={"errors": schema_errors})

        manifest_errors = []
        try:
            verify_plan_package(plan)
        except ArtifactPackageError as exc:
            manifest_errors.append(str(exc))
        fingerprint = compute_plan_package_fingerprint(plan)
        if approval.plan_version != plan_record["version"]:
            manifest_errors.append("approval plan version differs")
        if approval.plan_fingerprint != fingerprint:
            manifest_errors.append("approval package fingerprint differs")
        if deployment.plan_fingerprint != fingerprint:
            manifest_errors.append("deployment package fingerprint differs")
        add("manifest_digest_consistency", not manifest_errors,
            "Manifest, package, approval, and deployment fingerprints agree."
            if not manifest_errors else "Approved package integrity differs.",
            details={"errors": manifest_errors, "fingerprint": fingerprint})

        deployed_errors = []
        for artifact in artifacts:
            step = deployed.get(artifact.artifact_id)
            if not step:
                deployed_errors.append(f"missing:{artifact.artifact_id}")
            elif (step.content_digest != artifact.content_digest or
                  step.generated_definition != artifact.generated_definition):
                deployed_errors.append(f"modified:{artifact.artifact_id}")
        deployed_errors.extend(
            f"unexpected:{artifact_id}" for artifact_id in deployed if artifact_id not in by_id
        )
        add("deployed_definition_digest_consistency", not deployed_errors,
            "Deployed mock definitions and digests match the package." if not deployed_errors
            else "Deployed mock definitions differ from the package.",
            details={"errors": deployed_errors})

        summary = _summarize(checks)
        status = (ValidationStatus.FAILED if summary.failed else
                  ValidationStatus.PASSED_WITH_WARNINGS if summary.warnings else
                  ValidationStatus.PASSED)
        return StructuralValidationResult(
            discovery_id=discovery_record["id"], deployment_id=deployment_id,
            plan_id=deployment.plan_id, approval_id=deployment.approval_id,
            package_fingerprint=fingerprint, status=status,
            started_at=started, completed_at=_now(), summary=summary, checks=checks,
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summarize(checks: Iterable[StructuralValidationCheck]) -> StructuralValidationSummary:
    checks = list(checks)
    counts = Counter(check.status.value for check in checks)
    return StructuralValidationSummary(
        total_checks=len(checks), passed=counts[CheckStatus.PASSED.value],
        warnings=counts[CheckStatus.WARNING.value], failed=counts[CheckStatus.FAILED.value],
        skipped=counts[CheckStatus.SKIPPED.value],
        category_counts=dict(Counter(check.category for check in checks)),
    )


def _activity_names(activities: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for activity in activities:
        if activity.get("name"):
            names.add(activity["name"])
        properties = activity.get("properties", {})
        for key in ("ifTrueActivities", "ifFalseActivities", "activities"):
            nested = properties.get(key, [])
            if isinstance(nested, list):
                names.update(_activity_names(nested))
    return names


def _contains_value(value: Any, expected: Any) -> bool:
    if value == expected:
        return True
    if isinstance(value, dict):
        return any(_contains_value(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_value(item, expected) for item in value)
    return False


def _artifacts_by_source_name(artifacts):
    result = {}
    for artifact in artifacts:
        name = artifact.source_reference.split(":", 1)[-1]
        result.setdefault(name, artifact)
    return result
