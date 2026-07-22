"""Read-only Azure discovery API — Phase 9.

Exposes safe, read-only endpoints for checking Azure discovery
configuration and verifying the configured environment / Data Factory.
No secrets are ever returned. Verification is gated behind the enable
flag and full configuration; when disabled it returns 409 without any
Azure call.
"""

import logging

from fastapi import APIRouter, HTTPException

from src.config import get_settings
from src.connectors.azure_adf_client import (
    CODE_AUTH,
    CODE_AUTHZ,
    CODE_BOUNDARY,
    CODE_CONFIG,
    CODE_DISABLED,
    CODE_NOT_FOUND,
    CODE_TIMEOUT,
    AzureDiscoveryError,
)
from src.connectors.azure_adf_source import build_azure_adf_client_from_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/azure", tags=["azure"])

# Error code -> HTTP status (sanitized; no raw SDK detail is exposed).
_STATUS_MAP = {
    CODE_DISABLED: 409,
    CODE_CONFIG: 409,
    CODE_AUTH: 502,
    CODE_AUTHZ: 403,
    CODE_NOT_FOUND: 404,
    CODE_TIMEOUT: 504,
    CODE_BOUNDARY: 502,
}


def _raise_http(exc: AzureDiscoveryError):
    raise HTTPException(
        status_code=_STATUS_MAP.get(exc.code, 502),
        detail={"code": exc.code, "message": exc.message},
    )


@router.get("/status")
async def azure_status():
    """Report discovery configuration readiness. Never contacts Azure."""
    settings = get_settings()
    missing = settings.get_missing_azure_discovery_settings()
    return {
        "enabled": settings.enable_azure_discovery,
        "configured": not missing,
        "ready": settings.azure_discovery_ready(),
        "missing_settings": missing,
        "resource_group": settings.azure_resource_group or None,
        "data_factory_name": settings.azure_data_factory_name or None,
    }


@router.post("/verify")
async def azure_verify():
    """Read-only verification of the configured Azure environment + factory."""
    settings = get_settings()
    try:
        client = build_azure_adf_client_from_settings(settings)
        environment = client.verify_environment()
        data_factory = client.verify_data_factory()
        providers = {
            "Microsoft.DataFactory": client.provider_status("Microsoft.DataFactory"),
            "Microsoft.Storage": client.provider_status("Microsoft.Storage"),
        }
    except AzureDiscoveryError as exc:
        _raise_http(exc)
    return {
        "environment": environment,
        "data_factory": data_factory,
        "providers": providers,
    }
