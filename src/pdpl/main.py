"""FastAPI app factory. See ADR-0004."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from pdpl.api.health import router as health_router
from pdpl.api.tenants import router as tenants_router
from pdpl.config import get_settings
from pdpl.db.session import dispose_engine, get_engine
from pdpl.errors import install_exception_handlers
from pdpl.observability.correlation import CorrelationIdMiddleware
from pdpl.observability.logging import configure_logging, get_logger


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Eagerly build the engine so an unreachable DB / bad URL crashes here,
    # not on the first request. See ADR-0004 "Fail loud at boot".
    get_engine()
    log = get_logger("pdpl.lifecycle")
    log.info("app.startup")
    try:
        yield
    finally:
        log.info("app.shutdown")
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level=settings.log_level)

    app = FastAPI(
        title=settings.service_name,
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.add_middleware(CorrelationIdMiddleware)
    install_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(tenants_router)
    return app


app = create_app()
