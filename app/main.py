"""The ASGI app: settings, middleware, error handling, and the routers."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, domain, web, webhooks
from .config import Settings, load_settings
from .db import connect, init_schema
from .ratelimit import RateLimiter
from .routes import account, api, judging, organizer, public, teams
from .signing import Signer

STATIC = Path(__file__).with_name("static")

# The widget is the one page meant to be framed by other sites.
FRAMEABLE_PREFIX = "/embed/"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    conn = connect(settings.database_path)
    init_schema(conn)
    conn.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = webhooks.start(settings.database_path) if settings.webhooks else None
        yield
        if stop is not None:
            stop.set()

    app = FastAPI(
        lifespan=lifespan,
        title="Plumb",
        version=__version__,
        summary="A hackathon submission and judging portal whose results you can check.",
        docs_url=None,       # Swagger UI loads from a CDN; the portal must work offline.
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.signer = Signer.load_or_create(settings.signing_key_path)
    app.state.limiter = RateLimiter()

    @app.middleware("http")
    async def per_request(request: Request, call_next):
        try:
            response = await call_next(request)
        finally:
            conn = getattr(request.state, "conn", None)
            if conn is not None:
                conn.close()
        token = getattr(request.state, "set_anon_csrf", None)
        if token:
            response.set_cookie(web.ANON_CSRF_COOKIE, token, httponly=True, samesite="lax",
                                secure=settings.secure_cookies, max_age=60 * 60 * 24)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith(FRAMEABLE_PREFIX):
            response.headers.setdefault("Content-Security-Policy",
                                        "default-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors *")
        else:
            response.headers.setdefault("Content-Security-Policy",
                                        "default-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    @app.exception_handler(domain.DomainError)
    async def on_domain_error(request: Request, exc: domain.DomainError):
        return web.error_response(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException):
        message = "page not found" if exc.status_code == 404 else str(exc.detail)
        err = domain.DomainError(message)
        err.status = exc.status_code
        return web.error_response(request, err)

    @app.exception_handler(sqlite3.IntegrityError)
    async def on_integrity_error(request: Request, exc: sqlite3.IntegrityError):
        # The database refused an impossible state that the domain layer did
        # not catch first. Say so rather than failing with a 500.
        logging.getLogger("plumb").warning("integrity error on %s: %s", request.url.path, exc)
        err = domain.Conflict("that conflicts with existing data")
        return web.error_response(request, err)

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        # Keep only the parts that are always JSON-safe; "input" can be raw bytes.
        detail = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
                  for e in exc.errors()]
        return JSONResponse({"error": "invalid request", "detail": detail}, status_code=422)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    # organizer first: its /events/new and /events/import must win over /events/{event_id}.
    for module in (organizer, account, teams, judging, public, api):
        app.include_router(module.router)
    return app


def get_app() -> FastAPI:
    """Uvicorn factory entry point: uvicorn app.main:get_app --factory."""
    return create_app()
