"""FastAPI application — Phase 1.

Provides health check, config status, and root endpoints.
Initializes logging and database on startup.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from src.config import get_settings
from src.database import init_database
from src.logging_config import configure_logging
from src.api.routes import router as discovery_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # ── Startup ──────────────────────────────────────────────
    configure_logging()
    logger.info("Starting %s …", get_settings().app_name)
    try:
        init_database()
    except Exception as exc:
        logger.error("Database initialization failed: %s", exc)
        raise
    yield
    # ── Shutdown ─────────────────────────────────────────────
    logger.info("Shutting down %s.", get_settings().app_name)


app = FastAPI(
    title="Migration PoC API",
    description="Azure Data Factory → Microsoft Fabric migration tool",
    version="0.1.0",
    lifespan=lifespan,
)

# Register routers
app.include_router(discovery_router)


# ── Exception handler ────────────────────────────────────────────


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error("Unhandled error: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# ── Endpoints ────────────────────────────────────────────────────


@app.get("/")
async def root():
    """Service information."""
    settings = get_settings()
    return {
        "service": settings.app_name,
        "message": "Migration PoC API is running.",
    }


@app.get("/api/health")
async def health():
    """Health check endpoint."""
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.app_env,
    }


@app.get("/api/config/status")
async def config_status():
    """Return configuration status without exposing secrets."""
    settings = get_settings()
    missing = settings.get_missing_azure_settings()
    return {
        "configured": len(missing) == 0,
        "missing_settings": missing,
        "dry_run": settings.migration_dry_run,
        "approval_required": settings.migration_require_approval,
        "delete_allowed": settings.migration_allow_delete,
    }
