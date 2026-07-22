"""Structural validation, optional runtime checks, and report routes."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.reports.report_service import generate_report, report_path
from src.validation.mock_results import MockResultProvider
from src.validation.runtime_store import (
    get_latest_runtime_validation,
    save_runtime_validation,
)
from src.validation.structural_store import (
    get_latest_structural_validation,
    get_structural_validation,
    save_structural_validation,
)
from src.validation.structural_validator import (
    StructuralValidationError,
    StructuralValidationService,
)
from src.validation.validator import ValidationError, ValidationService

router = APIRouter(tags=["validation"])
FRONTEND_ROOT = Path(__file__).resolve().parent.parent.parent / "frontend"


class ValidationRunBody(BaseModel):
    deployment_id: int


@router.post("/api/validations/run")
async def run_validation(body: ValidationRunBody):
    """Run artifact structural validation only."""
    try:
        result = StructuralValidationService().validate(body.deployment_id)
    except StructuralValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    result = save_structural_validation(result)
    generate_report(result.validation_id)
    return result.model_dump(mode="json")


@router.get("/api/validations/latest")
async def latest_validation():
    result = get_latest_structural_validation()
    if not result:
        raise HTTPException(status_code=404, detail="No structural validation run yet.")
    return result.model_dump(mode="json")


@router.get("/api/validations/{validation_id}")
async def validation_by_id(validation_id: int):
    result = get_structural_validation(validation_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Validation {validation_id} not found.")
    return result.model_dump(mode="json")


@router.post("/api/runtime-validations/run")
async def run_runtime_validation(body: ValidationRunBody):
    """Run optional metrics; this never controls structural migration status."""
    provider = MockResultProvider()
    try:
        result = ValidationService().validate(
            body.deployment_id,
            provider.get_source_metrics(),
            provider.get_target_metrics(),
        )
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return save_runtime_validation(result).model_dump(mode="json")


@router.get("/api/runtime-validations/latest")
async def latest_runtime_validation():
    result = get_latest_runtime_validation()
    if not result:
        raise HTTPException(status_code=404, detail="No runtime validation run yet.")
    return result.model_dump(mode="json")


@router.get("/api/reports/{validation_id}.json")
async def get_report_json(validation_id: int):
    path = report_path(validation_id, "json")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Report not found.")
    return FileResponse(path, media_type="application/json")


@router.get("/api/reports/{validation_id}.html")
async def get_report_html(validation_id: int):
    path = report_path(validation_id, "html")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Report not found.")
    return FileResponse(path, media_type="text/html")


@router.get("/validation", include_in_schema=False)
async def validation_page():
    return FileResponse(FRONTEND_ROOT / "validation.html", media_type="text/html")


@router.get("/validation.js", include_in_schema=False)
async def validation_js():
    return FileResponse(FRONTEND_ROOT / "validation.js", media_type="text/javascript")
