"""Controlled execution + runtime-equivalence validation routes — Phase 11.

Every capability here is also directly callable in Python without FastAPI
(``src.execution.execution_service`` / ``src.validation.runtime_validation_service``),
so both direct usage and the HTTP workflow are first-class.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.connectors.azure_adf_executor import AzureExecutionError
from src.connectors.fabric_client import FabricError
from src.execution.execution_service import (
    ExecutionAuthorizationError,
    SourceExecutionService,
    TargetExecutionService,
    source_readiness,
    target_readiness,
)
from src.execution.execution_store import (
    DuplicateExecutionError,
    get_execution,
    list_executions,
)
from src.models.schemas import ExecutionSide, ExecutionStatus
from src.validation.runtime_execution_validation_store import (
    get_latest_runtime_execution_validation,
    get_runtime_execution_validation,
    save_runtime_execution_validation,
)
from src.validation.runtime_validation_service import (
    RuntimeValidationError,
    RuntimeValidationService,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["executions"])


class SourceExecutionStartBody(BaseModel):
    plan_id: Optional[int] = None
    deployment_id: Optional[int] = None
    discovery_snapshot_id: Optional[int] = None
    correlation_id: Optional[str] = None


class TargetExecutionStartBody(BaseModel):
    plan_id: int
    deployment_id: int
    correlation_id: Optional[str] = None


class RuntimeValidationStartBody(BaseModel):
    source_execution_id: int
    target_execution_id: int


@router.get("/api/executions/source-readiness")
async def get_source_readiness():
    """Report controlled source (ADF) execution readiness (no network)."""
    return source_readiness()


@router.get("/api/executions/target-readiness")
async def get_target_readiness():
    """Report controlled target (Fabric) execution readiness (no network)."""
    return target_readiness()


@router.post("/api/executions/source/start")
async def start_source_execution(body: SourceExecutionStartBody):
    """Start a controlled execution of the configured source ADF pipeline."""
    try:
        result = SourceExecutionService().start(
            plan_id=body.plan_id,
            deployment_id=body.deployment_id,
            discovery_snapshot_id=body.discovery_snapshot_id,
            correlation_id=body.correlation_id,
        )
    except AzureExecutionError as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": exc.message}
        )
    except DuplicateExecutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return result.model_dump(mode="json")


@router.post("/api/executions/target/start")
async def start_target_execution(body: TargetExecutionStartBody):
    """Start a controlled execution of the configured target Fabric pipeline.

    Requires full authorization: enabled runtime execution, a matching
    approved + unchanged package, a completed REAL deployment of the exact
    configured pipeline item, a matching read-back digest, and a passed
    structural validation for that deployment.
    """
    try:
        result = TargetExecutionService().start(
            plan_id=body.plan_id,
            deployment_id=body.deployment_id,
            correlation_id=body.correlation_id,
        )
    except ExecutionAuthorizationError as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": exc.message}
        )
    except FabricError as exc:
        raise HTTPException(
            status_code=502, detail={"code": exc.code, "message": exc.message}
        )
    except DuplicateExecutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return result.model_dump(mode="json")


@router.get("/api/executions/{execution_id}")
async def get_execution_by_id(execution_id: int):
    """Return one execution's safe metadata + metrics by id."""
    result = get_execution(execution_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found.")
    return result.model_dump(mode="json")


@router.get("/api/executions")
async def get_execution_history(
    side: Optional[ExecutionSide] = Query(default=None),
    plan_id: Optional[int] = Query(default=None),
    correlation_id: Optional[str] = Query(default=None),
    status: Optional[ExecutionStatus] = Query(default=None),
):
    """Return execution history, newest first, optionally filtered."""
    results = list_executions(
        side=side, plan_id=plan_id, correlation_id=correlation_id, status=status
    )
    return [result.model_dump(mode="json") for result in results]


@router.post("/api/executions/runtime-validation/start")
async def start_runtime_validation(body: RuntimeValidationStartBody):
    """Run the optional runtime-equivalence validation for a source/target
    execution pair. Never modifies structural validation status."""
    try:
        result = RuntimeValidationService().validate(
            body.source_execution_id, body.target_execution_id
        )
    except RuntimeValidationError as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": exc.message}
        )
    saved = save_runtime_execution_validation(result)
    return saved.model_dump(mode="json")


@router.get("/api/executions/runtime-validation/latest")
async def latest_runtime_validation(plan_id: Optional[int] = Query(default=None)):
    result = get_latest_runtime_execution_validation(plan_id=plan_id)
    if result is None:
        raise HTTPException(status_code=404, detail="No runtime validation run yet.")
    return result.model_dump(mode="json")


@router.get("/api/executions/runtime-validation/{validation_id}")
async def runtime_validation_by_id(validation_id: int):
    result = get_runtime_execution_validation(validation_id)
    if result is None:
        raise HTTPException(
            status_code=404, detail=f"Runtime validation {validation_id} not found."
        )
    return result.model_dump(mode="json")
