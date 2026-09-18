import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from auth.setup import bootstrap_setup_token
from auth.setup import router as setup_router
from core.config import get_settings
from core.db import SessionDep, get_engine, get_purge_engine, session_scope
from core.logging import configure_logging

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


@api.get("/health")
async def health(session: SessionDep) -> JSONResponse:
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        # Any DB failure is "degraded" to the caller; the cause goes to the log, never the body.
        log.exception("health: database unreachable")
        return JSONResponse({"status": "degraded", "database": "unreachable"}, status_code=503)
    return JSONResponse({"status": "ok", "database": "ok"})


api.include_router(setup_router)
app.include_router(api)
