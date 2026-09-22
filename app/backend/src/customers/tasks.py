"""The purge half of erasure: the only code that runs as `linsuite_purge` (pre-flight D5a).

The request handler (`customers/erasure.py`, app role) has already removed everything the
app role may remove and suppressed the profile. What is left needs the purge role — deleting
the client's `customer_document_keys` row, the crypto-shred of ADR-0001 §5 — and so runs
here, in a Celery task, never in a request.

**One purge transaction, in a fixed order:** the `customer.key_destroyed` audit row, then
the client's documents (their FK to the key row forces it), then the key. All or nothing:
an erasure with no audit row, or an audit row for an erasure that did not happen, cannot be
committed. Nothing is written when there is nothing to delete, so re-running is
silent.

**Eligibility is the database's.** The key's guard trigger (0024) refuses the DELETE while
the client is under a hold — `retention_expires_at` in the future or `'infinity'`. That
refusal is caught and means "held, try again another night", never an error. The job's own
queries only choose whom to try; they are not what makes a purge safe.

**Anonymising is the app role's.** The purge role holds UPDATE on nothing and must keep it
that way (`test_schema.py`). So once the key is gone, an app-role transaction re-reads the
hold under the customer row's lock, replaces names and DOB, and stamps `purged_at` — only if
the key really is gone *and* nothing holds the client now. A missing key alone proves
nothing: a client from before 0024 never had one.

**Idempotent, by construction.** Every step is conditional on the state it changes, so
running any of this twice — after a crash, or after a restore brought wrapped keys back from
a backup (the runbook's action: run `purge_expired`) — does the remaining work and no more.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.celery_app import celery_app
from core.db import get_task_engines
from customers.models import ALWAYS_ERASED, ERASED_NAMES

log = logging.getLogger(__name__)

PURGE_AUTHORITY = "linsuite_purge"
# The guards' own refusals — the key's (0024) and the shared record guard on `documents`
# (0026). Any other `insufficient_privilege` — a grant gone missing — is a real fault and must
# not be mistaken for "held" night after night.
_GUARD_REFUSALS = (
    "customer_document_keys: DELETE is not permitted",
    "documents: DELETE is not permitted",
)
# The trigger's predicate, verbatim: not held = no hold, or a hold that has passed.
_NOT_HELD = "(retention_expires_at IS NULL OR retention_expires_at < now())"


def _run(work: Callable[..., Awaitable[Any]], *args: object) -> Any:
    """`asyncio.run`, from a worker (no loop running) or from an eager call made inside a
    request handler in the test suite (a loop is running there, so use a thread's own)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(work(*args))
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, work(*args)).result()


async def _shred(purge: AsyncEngine, customer_id: str, request_id: str | None) -> bool:
    """Destroy one client's key, audited, in one purge transaction. True if a key went."""
    metadata = json.dumps({"authority": PURGE_AUTHORITY, "request_id": request_id})
    async with purge.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO audit_events (event_type, target_type, target_id, metadata) "
                    "VALUES ('customer.key_destroyed', 'customer', :c, cast(:m AS jsonb))"
                ),
                {"c": customer_id, "m": metadata},
            )
            # The client's sealed rows, after the audit row and before the key they are
            # sealed under (ADR-0001 rule 8) — their FK to the key row forces this order.
            # Task 4: `DELETE FROM form_submissions` goes here too, beside documents.
            await conn.execute(
                text("DELETE FROM documents WHERE customer_id = :c"), {"c": customer_id}
            )
            deleted = (
                await conn.execute(
                    text("DELETE FROM customer_document_keys WHERE customer_id = :c"),
                    {"c": customer_id},
                )
            ).rowcount
        except DBAPIError as error:
            await transaction.rollback()
            if any(refusal in str(error) for refusal in _GUARD_REFUSALS):
                return False  # held: the trigger said no, which is its job
            raise
        if deleted:
            await transaction.commit()
        else:
            await transaction.rollback()  # nothing to destroy, so nothing to record
        return bool(deleted)


async def _finish(app: AsyncEngine, purge: AsyncEngine, request_id: str) -> bool:
    """Complete one request if nothing holds it. True once `purged_at` is stamped."""
    async with app.connect() as conn:
        customer_id = await conn.scalar(
            text("SELECT customer_id FROM erasure_requests WHERE id = :r AND purged_at IS NULL"),
            {"r": request_id},
        )
    if customer_id is None:
        return False  # unknown, or already finished
    await _shred(purge, str(customer_id), request_id)

    cleared = ", ".join(f"{column} = NULL" for column in ALWAYS_ERASED)
    async with app.begin() as conn:
        # The hold as it is now, under the row lock every retention writer also takes.
        not_held = await conn.scalar(
            text(f"SELECT {_NOT_HELD} FROM customers WHERE id = :c FOR UPDATE"),
            {"c": customer_id},
        )
        keyed = await conn.scalar(
            text("SELECT EXISTS (SELECT 1 FROM customer_document_keys WHERE customer_id = :c)"),
            {"c": customer_id},
        )
        if not not_held or keyed:
            return False
        await conn.execute(
            text(
                f"UPDATE customers SET first_name = :first, last_name = :last, "
                f"date_of_birth = NULL, {cleared}, updated_at = now() WHERE id = :c"
            ),
            {"first": ERASED_NAMES[0], "last": ERASED_NAMES[1], "c": customer_id},
        )
        await conn.execute(
            text("UPDATE erasure_requests SET purged_at = now() WHERE id = :r"), {"r": request_id}
        )
    return True


async def _finish_one(request_id: str) -> bool:
    app, purge = get_task_engines()
    try:
        return await _finish(app, purge, request_id)
    finally:
        await app.dispose()
        await purge.dispose()


async def _purge_expired() -> dict[str, int]:
    app, purge = get_task_engines()
    counts = {"requests_finished": 0, "keys_destroyed": 0, "failed": 0}
    try:
        # 1. Requests whose hold is gone (or never was, and the enqueue was lost).
        async with app.connect() as conn:
            pending = list(
                await conn.scalars(
                    text(
                        "SELECT r.id FROM erasure_requests r "
                        "JOIN customers c ON c.id = r.customer_id "
                        "WHERE r.purged_at IS NULL AND "
                        "(c.retention_expires_at IS NULL OR c.retention_expires_at < now())"
                    )
                )
            )
        for request_id in pending:
            try:
                counts["requests_finished"] += await _finish(app, purge, str(request_id))
            except Exception:
                counts["failed"] += 1
                log.exception("erasure request %s could not be finished", request_id)
        # 2. Expired holds, request or not: the key goes, the profile stays (owner-confirmed
        # — nobody is anonymised who did not ask). NULL holds are never swept: not held is
        # not the same as expired, and a salon client who never asked keeps their key.
        async with app.connect() as conn:
            expired = list(
                await conn.scalars(
                    text(
                        "SELECT c.id FROM customers c "
                        "JOIN customer_document_keys k ON k.customer_id = c.id "
                        "WHERE c.retention_expires_at < now()"
                    )
                )
            )
        for customer_id in expired:
            try:
                counts["keys_destroyed"] += await _shred(purge, str(customer_id), None)
            except Exception:
                counts["failed"] += 1
                log.exception("key for customer %s could not be destroyed", customer_id)
    finally:
        await app.dispose()
        await purge.dispose()
    log.info("purge_expired", extra=counts)
    return counts


@celery_app.task(name="customers.tasks.finish_erasure")
def finish_erasure(request_id: str) -> bool:
    """Enqueued by `POST /customers/{id}/erasure` after its commit. A held client is left
    for `purge_expired`; a lost enqueue is picked up by it too."""
    return _run(_finish_one, request_id)


@celery_app.task(name="customers.tasks.purge_expired")
def purge_expired() -> dict[str, int]:
    """Nightly from beat (`core/celery_app.py`). Safe to run by hand at any time — and it is
    the runbook's step after any database restore."""
    return _run(_purge_expired)
