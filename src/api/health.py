import logging

from fastapi import APIRouter
from sqlalchemy import text

from src.db import engine

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    """Basic health check with dependency status."""
    db_status = "unknown"
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {e}"
        logger.error("Health check DB error: %s", e)

    status = "healthy" if db_status == "connected" else "degraded"
    return {
        "status": status,
        "version": "0.1.0",
        "database": db_status,
    }


@router.get("/health/ready")
async def readiness():
    """Readiness check for Cloud Run — returns 503 if not ready."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as e:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=503, content={"status": "not_ready", "error": str(e)}
        )
