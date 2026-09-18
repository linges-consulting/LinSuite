import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from auth.login import router as auth_router
from auth.passwords import router as passwords_router
from auth.setup import bootstrap_setup_token
from auth.setup import router as setup_router
from auth.throttle import router as throttle_router
from core.business import router as business_router
from core.config import get_settings
from core.db import SessionDep, get_engine, get_purge_engine, session_scope
from core.logging import configure_logging
from core.redis import get_redis

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
_MUTATING = ("POST", "PUT", "PATCH", "DELETE")


@app.middleware("http")
async def require_json_body(request: Request, call_next):
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if request.method in _MUTATING and content_type != "application/json":
        return JSONResponse({"detail": "Send application/json"}, status_code=415)
    return await call_next(request)


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
api.include_router(passwords_router)
api.include_router(setup_router)
api.include_router(throttle_router)
api.include_router(business_router)
app.include_router(api)
