"""Global exception handler — clean JSON, correlation ID included."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from pdpl.observability.correlation import current_correlation_id
from pdpl.observability.logging import get_logger

_log = get_logger("pdpl.errors")


def _error_body(message: str, *, code: str) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "correlation_id": str(current_correlation_id()),
        }
    }


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        _log.warning(
            "http.error",
            status=exc.status_code,
            detail=str(exc.detail),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(str(exc.detail), code=f"http_{exc.status_code}"),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        _log.warning("http.validation_error", errors=exc.errors())
        # jsonable_encoder mirrors FastAPI's own handler: a custom validator
        # that raises ValueError puts the (non-serialisable) exception object
        # in each error's `ctx`, which a raw json.dumps would choke on (500).
        return JSONResponse(
            status_code=422,
            content=_error_body("Validation failed.", code="validation_error")
            | {"details": jsonable_encoder(exc.errors())},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        _log.exception("http.unhandled_exception", error_class=type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content=_error_body("Internal server error.", code="internal_error"),
        )
