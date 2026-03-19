import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.db import engine

# Configure logging so our app's INFO messages are visible
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s - %(message)s")
# Quiet down noisy libraries
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown hooks."""
    logger.info("Radar starting up")
    # Verify database connectivity
    try:
        async with engine.connect() as conn:
            from sqlalchemy import text

            await conn.execute(text("SELECT 1"))
        logger.info("Database connected")
    except Exception as e:
        logger.error("Database connection failed: %s", e)

    yield

    # Shutdown
    await engine.dispose()
    logger.info("Radar shut down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Radar",
        description="WhatsApp-native AI assistant for family activity coordination",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS — minimal, only needed for OAuth callback page
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # Register routers
    from src.api.health import router as health_router

    app.include_router(health_router)

    # Conditionally import routers that may not exist yet during development
    try:
        from src.api.webhooks import router as webhooks_router

        app.include_router(webhooks_router)
    except (ImportError, Exception) as e:
        logger.warning("Webhooks router not available: %s", e)

    try:
        from src.api.oauth import router as oauth_router

        app.include_router(oauth_router)
    except (ImportError, Exception) as e:
        logger.warning("OAuth router not available: %s", e)

    try:
        from src.api.internal import router as internal_router

        app.include_router(internal_router)
    except (ImportError, Exception) as e:
        logger.warning("Internal router not available: %s", e)

    return app


app = create_app()
