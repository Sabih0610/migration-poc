"""Small valid generated-package plans for approval/deployment unit tests."""

from src.artifacts import build_package
from src.models.schemas import (
    DeployableTargetType,
    GeneratedArtifact,
    MigrationAction,
    MigrationActionType,
    MigrationPlan,
    MigrationRisk,
    TargetItemType,
)


def make_package_plan(executable: bool = True, destructive: bool = False):
    artifact = GeneratedArtifact(
        artifact_id="connection:test",
        source_reference="linked_service:test",
        target_type=DeployableTargetType.CONNECTION,
        target_name="test",
        generated_definition={
            "type": "FabricConnection",
            "name": "test",
            "properties": {
                "connectionType": "AzureDataLakeStorageGen2",
                "endpoint": "https://example.invalid",
                "authentication": {"kind": "ManagedIdentity"},
            },
        },
        content_digest="",
    )
    reason = "delete the old staging table" if destructive else "verify"
    return MigrationPlan(
        executable=executable,
        overall_risk=MigrationRisk.MEDIUM,
        actions=[
            MigrationAction(
                order=1,
                action_type=MigrationActionType.VERIFY_WORKSPACE,
                target_item_type=TargetItemType.WORKSPACE,
                target_item_name="ws",
                reason=reason,
            )
        ],
        generated_package=build_package([artifact]),
    )
