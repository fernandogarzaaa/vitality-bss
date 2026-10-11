"""FastAPI application factory."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from vitality_bss import __version__
from vitality_bss.config import get_settings
from vitality_bss.logging_setup import configure_logging, get_logger, request_id_var
from vitality_bss.web import api, pages
from vitality_bss.web.deps import LoginRequired

log = get_logger("vitality_bss.http")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
UNLOGGED_PATHS = frozenset({"/healthz", "/readyz"})


def startup() -> None:
    """Validate config, migrate, bootstrap the first admin and optional demo data."""
    from vitality_bss import db as dbmod
    from vitality_bss import services

    s = get_settings()
    configure_logging(s.LOG_LEVEL, s.LOG_FORMAT)
    for warning in s.validate_for_runtime():
        log.warning(warning)
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    if s.AUTO_MIGRATE:
        dbmod.run_migrations()
    with dbmod.new_session() as session:
        admin = services.bootstrap_admin(session)
        if admin is not None:
            log.info("bootstrap admin created", extra={"user_id": admin.id})
        if s.SEED_DEMO_DATA:
            from vitality_bss.demo import seed_demo

            seed_demo(session)


def apply_security_headers(response: Response, path: str, secure: bool) -> None:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if not path.startswith("/api/docs"):  # Swagger UI needs its CDN assets
        response.headers["Content-Security-Policy"] = CSP
    if not path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store"
    if secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"


def create_app(run_startup: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if run_startup:
            startup()
        yield

    s = get_settings()
    app = FastAPI(
        title=s.APP_NAME,
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(api.health_router)
    app.include_router(api.router)
    app.include_router(pages.router)

    max_body = s.max_upload_bytes * s.MAX_FILES_PER_REQUEST + 1024 * 1024

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = "".join(ch for ch in request.headers.get("X-Request-ID", "")[:64] if ch.isalnum() or ch in "-_")
        rid = rid or uuid.uuid4().hex
        token = request_id_var.set(rid)
        start = time.perf_counter()
        try:
            length = request.headers.get("content-length", "")
            if length.isdigit() and int(length) > max_body:
                response: Response = JSONResponse({"detail": "Request body too large"}, status_code=413)
            else:
                response = await call_next(request)
        except Exception:
            log.exception("unhandled error", extra={"path": request.url.path})
            response = JSONResponse({"detail": "Internal server error", "request_id": rid}, status_code=500)
        response.headers["X-Request-ID"] = rid
        apply_security_headers(response, request.url.path, get_settings().cookie_secure)
        if request.url.path not in UNLOGGED_PATHS:
            log.info(
                "request",
                extra={"method": request.method, "path": request.url.path, "status": response.status_code,
                       "duration_ms": round((time.perf_counter() - start) * 1000, 1)},
            )
        request_id_var.reset(token)
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _: LoginRequired) -> Response:
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> Response:
        wants_html = not request.url.path.startswith("/api") and "text/html" in request.headers.get("accept", "")
        if wants_html:
            ctx = pages.base_context(request, None)
            ctx.update(status_code=exc.status_code, detail=exc.detail)
            return pages.templates.TemplateResponse(request, "error.html", ctx, status_code=exc.status_code)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
        # Deliberately omit the submitted input: it may contain PHI.
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    return app
