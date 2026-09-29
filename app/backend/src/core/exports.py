"""Shared report-export mechanism (#86, M5 spec #82).

The commission CSV export queue (R29) generalises into one mechanism any report kind can
use: one `report_exports` table (`core/models.py::ReportExport`), one Celery task, one
7-day expiry, one audit event. `core/` imports no domain module, so the direction has to run
the other way: a domain module imports this one and calls `register_builder` at import time
to hand over the one function that turns its own `params` into CSV text. `core.celery_app`'s
`include` list is what guarantees a worker has actually imported that domain module before
anything calls `build_report_export`.

**Adding a kind needs exactly two things**, both in the domain module that owns it:

    async def build_my_export(db: AsyncSession, params: dict[str, Any]) -> str: ...
    register_builder("my_kind", build_my_export)

and an endpoint that calls `request_export(db, kind="my_kind", params=..., requested_by=...)`
then, after `db.commit()`, `build_report_export.delay(str(export.id))` — see
`billing/commission_report.py` for the shape a request/poll/download trio takes around it,
and `billing/package_liability.py` (#88) for a kind that filters by, and audits reads of, a
customer.

A kind whose `params` names a customer stores that id under `params["customer_id"]`
(`delete_customer_exports` below matches on exactly that key) — ADR-0001 rule 14: the export
is deleted the moment that customer's erasure is requested, in the same transaction.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from core.audit import record_event
from core.celery_app import celery_app
from core.config import get_settings
from core.db import run_task
from core.models import ReportExport

# The 7-day expiry itself lives as `report_exports.expires_at`'s DB-level default
# (`now() + interval '7 days'`, migration 0069) — one source of truth, so a raw INSERT (a
# test, a future domain) gets the same rule the ORM path does without repeating it here.

Builder = Callable[[AsyncSession, dict[str, Any]], Awaitable[str]]

_builders: dict[str, Builder] = {}


def register_builder(kind: str, builder: Builder) -> None:
    """Called once, at import time, by the domain module that owns `kind`."""
    _builders[kind] = builder


def is_registered(kind: str) -> bool:
    return kind in _builders


def is_expired(export: ReportExport, *, now: datetime | None = None) -> bool:
    return (now or datetime.now(UTC)) >= export.expires_at


async def request_export(
    db: AsyncSession, *, kind: str, params: dict[str, Any], requested_by: uuid.UUID
) -> ReportExport:
    """Stages the row and its `report.export_requested` audit event in the caller's own
    transaction — the caller commits, and only after that commit queues
    `build_report_export.delay(str(export.id))`, never before (R29's own rule: the worker
    must find the row it was handed)."""
    export = ReportExport(kind=kind, params=params, requested_by=requested_by)
    db.add(export)
    await db.flush()
    record_event(
        db,
        "report.export_requested",
        target_type="report_export",
        target_id=str(export.id),
        actor_user_id=requested_by,
        # The kind and its filters, never content (ADR-0001 §6) — the CSV itself is built
        # later, in a separate transaction, by the worker.
        metadata={"kind": kind, "params": params},
    )
    return export


async def delete_customer_exports(db: AsyncSession, customer_id: uuid.UUID) -> None:
    """ADR-0001 rule 14: an export whose `params` names a customer is deleted the moment
    that customer's erasure is requested — a copy, not a record under retention. Every kind
    that filters by one client stores that id under the same `params["customer_id"]` key (a
    `package_liability` export, #88; an `access_log` export, #87) so this one helper matches
    both without knowing either kind. Call inside the erasure request's own transaction —
    the caller's `commit` is what makes the delete durable, same as everything else there."""
    await db.execute(
        delete(ReportExport).where(ReportExport.params["customer_id"].astext == str(customer_id))
    )


@celery_app.task(name="core.exports.build_report_export")
def build_report_export(export_id: str) -> None:
    """Queued once, after the request that created the export has committed. One task
    builds every kind: it looks up the registered builder for the row's own `kind` and
    contains no report-specific logic itself."""
    run_task(_build_export, uuid.UUID(export_id))


async def _build_export(export_id: uuid.UUID) -> None:
    # A fresh engine for this task's own event loop, application role only — the shape
    # `billing/documents.py::_render_invoice_documents` uses.
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            export = await db.get(ReportExport, export_id)
            if export is None or export.status != "pending":
                return
            try:
                builder = _builders[export.kind]
                export.content = await builder(db, export.params)
                export.status = "ready"
            except Exception:
                await db.rollback()
                export = await db.get(ReportExport, export_id)
                assert export is not None
                export.status = "failed"
                raise
            finally:
                export.completed_at = datetime.now(UTC)
                await db.commit()
    finally:
        await engine.dispose()
