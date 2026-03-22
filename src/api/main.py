import asyncio
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

        # Clear stale pending actions so they don't confuse intent classification
        from src.db import async_session_factory
        from src.state.pending import expire_all_pending

        async with async_session_factory() as session:
            expired = await expire_all_pending(session)
            await session.commit()
            if expired:
                logger.info("Expired %d stale pending actions on startup", expired)
    except Exception as e:
        logger.error("Database connection failed: %s", e)

    # Start background processors
    background_tasks: list[asyncio.Task] = []
    try:
        from src.actions.gcal_outbox_processor import process_outbox_loop

        outbox_task = asyncio.create_task(process_outbox_loop())
        background_tasks.append(outbox_task)
    except Exception as e:
        logger.warning("Could not start outbox processor: %s", e)

    try:
        from src.actions.gcal_reconciler import reconcile_all_families, reconcile_loop

        reconciler_task = asyncio.create_task(reconcile_loop())
        background_tasks.append(reconciler_task)

        # Run immediate reconciliation on startup so local DB is fresh
        async def _startup_reconcile():
            try:
                stats = await reconcile_all_families()
                logger.info("Startup reconciliation complete: %s", stats)
            except Exception as exc:
                logger.warning("Startup reconciliation failed (non-fatal): %s", exc)

        asyncio.create_task(_startup_reconcile())
    except Exception as e:
        logger.warning("Could not start GCal reconciler: %s", e)

    yield

    # Shutdown: cancel background tasks
    for task in background_tasks:
        task.cancel()
    for task in background_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass

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
