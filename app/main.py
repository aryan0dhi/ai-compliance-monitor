"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health, v1
from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    test_routes = settings.test_routes_enabled

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Runs when the server starts (not in unit tests, which don't enter the lifespan).
        if test_routes:
            from app.modules.testing.clock import install_simulated_clock

            install_simulated_clock()
        yield

    app = FastAPI(
        title="AI Compliance Monitor",
        version="0.1.0",
        description="Post-market monitoring for high-risk AI systems (EU AI Act Art. 72-73).",
        lifespan=lifespan,
    )
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(v1.build_router(include_test_routes=test_routes))
    if test_routes:
        # Course cross-team testing interface, at the service root (D-19).
        from app.modules.testing.session_router import router as session_router

        app.include_router(session_router)
    return app


app = create_app()
