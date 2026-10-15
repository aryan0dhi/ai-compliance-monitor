"""FastAPI application factory."""

from fastapi import FastAPI

from app.api import health, v1
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="AI Compliance Monitor",
        version="0.1.0",
        description="Post-market monitoring for high-risk AI systems (EU AI Act Art. 72-73).",
    )
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(v1.router)
    return app


app = create_app()
