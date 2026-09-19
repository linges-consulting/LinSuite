import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from auth.admin_users import router as admin_users_router
from auth.login import router as auth_router
from auth.mfa_routes import router as mfa_router
from auth.passwords import router as passwords_router
from auth.roles import router as roles_router
from auth.setup import bootstrap_setup_token
from auth.setup import router as setup_router
from core.config import get_settings
from core.db import SessionDep, get_engine, get_purge_engine, session_scope
from core.errors import UPLOAD_ORIGIN_REQUIRED, Forbidden
from core.logging import configure_logging
from core.redis import get_redis
from settings.images import FAVICON_MAX_BYTES, LOGO_MAX_BYTES
from settings.routes import public as branding_router
from settings.routes import router as business_router

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging(get_settings().log_level)
    # A fresh instance mints a setup token on every boot until the wizard is completed.
    async with session_scope() as session:
        await bootstrap_setup_token(session)
    yield
    await get_engine().dispose()
    await get_purge_engine().dispose()
    await get_redis().aclose()


app = FastAPI(
    title="LinSuite", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json"
)
api = APIRouter(prefix="/api")

# What the client sent stays out of the response. FastAPI's default 422 body echoes the
# offending value in `input`, which would put a rejected password in every proxy log, HAR
# export and error tracker that sees the response. `loc` says which field; that is enough.
_SAFE_ERROR_KEYS = ("type", "loc", "msg")


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    detail = [{k: e[k] for k in _SAFE_ERROR_KEYS if k in e} for e in exc.errors()]
    return JSONResponse({"detail": detail}, status_code=422)


# Three different things answer 403 — a lapsed Admin Mode window, a capability the role does
# not hold, and a password change owed — and the browser has to respond to each differently.
# `code` is what it switches on; `detail` stays the sentence a person reads. Starlette walks
# the exception's MRO, so this handler wins over the generic `HTTPException` one.
@app.exception_handler(Forbidden)
async def forbidden(_: Request, exc: Forbidden) -> JSONResponse:
    return JSONResponse({"detail": exc.detail, "code": exc.code}, status_code=403)


# Half of the CSRF defence for the session cookie; `SameSite=Lax` is the other half.
#
# An allowlist, not a denylist. `application/json` is the one content type a cross-origin
# page cannot send without a CORS preflight it will not survive — so requiring it is the
# defence. Refusing only the three form encodings would let through the case that matters
# most: `fetch(url, {method: 'POST', credentials: 'include'})` with no body sends no
# `Content-Type` at all, is a CORS simple request, and would sail past a denylist, leaving
# `SameSite=Lax` as the single point of failure this is here to remove.
#
# A middleware rather than a per-route dependency, precisely so a future endpoint cannot
# forget it. Every mutation the frontend makes goes through one `post()` helper that always
# sets the header, including bodiless ones like logout.
#
# Two paths are exempt, named here and nowhere else: a file cannot be uploaded as JSON, and a
# base64 field would mean holding the whole image in memory twice to save a header. In
# exchange those two paths get the check JSON was standing in for — the `Origin` has to be
# this deployment. `SameSite=Lax` already blocks a cross-site form POST; this is the belt to
# its braces, and it is why the exemption is a fixed tuple rather than a prefix.
_MUTATING = ("POST", "PUT", "PATCH", "DELETE")
_UPLOADS: dict[str, int] = {
    "/api/admin/business/logo": LOGO_MAX_BYTES,
    "/api/admin/business/favicon": FAVICON_MAX_BYTES,
}
# Multipart headers, boundaries and the filename, generously. The real cap is applied to the
# file itself in `settings/routes.py`; this one is here to refuse an oversized body before it
# is read at all.
_MULTIPART_OVERHEAD = 4096


@app.middleware("http")
async def require_json_body(request: Request, call_next):
    if request.method not in _MUTATING:
        return await call_next(request)
    cap = _UPLOADS.get(request.url.path) if request.method == "POST" else None
    if cap is not None:
        if not _from_this_deployment(request):
            # Coded like every other 403 (`core/errors.py`). Built by hand rather than raised
            # as `Forbidden`, because this runs *outside* `ExceptionMiddleware` and the
            # handler registered for it would never see the exception.
            return JSONResponse(
                {"detail": "Upload from this application.", "code": UPLOAD_ORIGIN_REQUIRED},
                status_code=403,
            )
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cap + _MULTIPART_OVERHEAD:
            return JSONResponse({"detail": f"Keep it under {cap // 1024} KB."}, status_code=413)
        return await call_next(request)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        return JSONResponse({"detail": "Send application/json"}, status_code=415)
    return await call_next(request)


def _from_this_deployment(request: Request) -> bool:
    """`Origin`, or the `Referer` it is derived from when a browser withholds it."""
    origin = request.headers.get("origin")
    if origin is None:
        referer = request.headers.get("referer")
        origin = _origin_of(referer) if referer else None
    return origin is not None and origin == _origin_of(get_settings().app_base_url)


def _origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


@api.get("/health")
async def health(session: SessionDep) -> JSONResponse:
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        # Any DB failure is "degraded" to the caller; the cause goes to the log, never the body.
        log.exception("health: database unreachable")
        return JSONResponse({"status": "degraded", "database": "unreachable"}, status_code=503)
    return JSONResponse({"status": "ok", "database": "ok"})


api.include_router(auth_router)
api.include_router(mfa_router)
api.include_router(passwords_router)
api.include_router(setup_router)
api.include_router(roles_router)
api.include_router(admin_users_router)
api.include_router(business_router)
api.include_router(branding_router)
app.include_router(api)
