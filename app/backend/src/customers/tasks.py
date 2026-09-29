"""The purge half of erasure: the only code that runs as `linsuite_purge` (pre-flight D5a).

The request handler (`customers/erasure.py`, app role) has already removed everything the
app role may remove and suppressed the profile. What is left needs the purge role — deleting
the client's `customer_document_keys` row, the crypto-shred of ADR-0001 §5 — and so runs
here, in a Celery task, never in a request.

**One purge transaction, in a fixed order:** the `customer.key_destroyed` audit row, then
the client's documents and form submissions (their FK to the key row forces it), then the
key. All or nothing: an erasure with no audit row, or an audit row for an erasure that did
not happen, cannot be committed. Nothing is written when there is nothing to delete, so
re-running is silent.

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

**A third, unrelated step (#84; ADR-0001 amendment) rides along in the same nightly run:**
business-keyed financial documents whose own CRA `retain_until` has passed and whose linked
client, if any, is not under a clinical hold. Nothing is crypto-shredded here — the business
key never shreds (ADR-0003 §5) — a `document.purged` audit row, then the row's own DELETE, is
the erasure. Same guard-refusal-means-held pattern as the key shred, same idempotence.
"""

import json
import logging

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.celery_app import celery_app
from core.db import get_task_engines, run_task
from core.healthcheck import ping_maintenance
from customers.models import ALWAYS_ERASED, ERASED_NAMES

log = logging.getLogger(__name__)

PURGE_AUTHORITY = "linsuite_purge"
# The guards' own refusals — the key's (0024) and the shared record guard on `documents`
# (0026) — matched as the exact table-qualified start of the message, so another table whose
# name merely ends the same way cannot pass for either. Any other `insufficient_privilege` — a
# grant gone missing — is a real fault and must not be mistaken for "held" night after night.
_GUARD_REFUSALS = tuple(
    f"{table}: DELETE is not permitted for "
    for table in ("customer_document_keys", "documents", "form_submissions", "session_notes")
)
_FOREIGN_KEY_VIOLATION = "23503"
# Every foreign key to `customer_document_keys` — the rows sealed under a client's key. The key
# DELETE failing on one of these, and only these, means a sealed row committed mid-purge
# (ADR-0001 rule 7). `test_the_shred_hook_knows_every_foreign_key_to_the_key_row` keeps the
# set equal to the database's. Any other FK violation is a real fault and raises.
KEY_REFERENCES = frozenset(
    {
        "documents_customer_id_fkey",
        "form_submissions_customer_id_fkey",
        "session_notes_customer_id_fkey",
    }
)
# The trigger's predicate, verbatim: not held = no hold, or a hold that has passed.
_NOT_HELD = "(retention_expires_at IS NULL OR retention_expires_at < now())"


def _refused_by_guard(message: str) -> bool:
    return message.startswith(_GUARD_REFUSALS)


def _inserted_mid_purge(sqlstate: str | None, constraint: str | None) -> bool:
    return sqlstate == _FOREIGN_KEY_VIOLATION and constraint in KEY_REFERENCES


def _postgres(error: DBAPIError) -> tuple[str | None, str, str | None]:
    """(SQLSTATE, the server's own message, the constraint it names) — asyncpg's error is a
    cause down."""
    cause = getattr(error.orig, "__cause__", None)
    return (
        getattr(error.orig, "sqlstate", None),
        str(cause or ""),
        getattr(cause, "constraint_name", None),
    )


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
            # Every table in `KEY_REFERENCES` is deleted from here.
            for table in ("documents", "form_submissions", "session_notes"):
                await conn.execute(
                    text(f"DELETE FROM {table} WHERE customer_id = :c"), {"c": customer_id}
                )
            deleted = (
                await conn.execute(
                    text("DELETE FROM customer_document_keys WHERE customer_id = :c"),
                    {"c": customer_id},
                )
            ).rowcount
        except DBAPIError as error:
            await transaction.rollback()
            sqlstate, message, constraint = _postgres(error)
            if _refused_by_guard(message):
                return False  # held: the trigger said no, which is its job
            if _inserted_mid_purge(sqlstate, constraint):
                # A document or submission committed for this client while the purge ran (it
                # waited on the insert's lock on the key row, ADR-0001 rule 7). Nothing was
                # destroyed; the next run deletes it with the rest.
                log.warning(
                    "key for customer %s gained a sealed row mid-purge (%s); deferred",
                    customer_id,
                    constraint,
                )
                return False
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


async def _purge_document(purge: AsyncEngine, document_id: str, kind: str) -> bool:
    """Destroy one eligible business-keyed financial document (#84; ADR-0001 amendment),
    audited first, in its own purge transaction — the same audit-then-delete order as `_shred`.

    The guard's own read of `retain_until` and any linked client's hold is what actually
    decides eligibility; this function's caller only selected a candidate to try. A guard
    refusal means "held, try another night", never an error. True if the document went."""
    metadata = json.dumps({"authority": PURGE_AUTHORITY, "kind": kind, "document_id": document_id})
    async with purge.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO audit_events (event_type, target_type, target_id, metadata) "
                    "VALUES ('document.purged', 'document', :d, cast(:m AS jsonb))"
                ),
                {"d": document_id, "m": metadata},
            )
            deleted = (
                await conn.execute(text("DELETE FROM documents WHERE id = :d"), {"d": document_id})
            ).rowcount
        except DBAPIError as error:
            await transaction.rollback()
            _, message, _ = _postgres(error)
            if _refused_by_guard(message):
                return False  # held: the guard said no, which is its job
            raise
        if deleted:
            await transaction.commit()
        else:
            await transaction.rollback()  # already gone: a re-run, nothing to record
        return bool(deleted)


async def _purge_expired() -> dict[str, int]:
    app, purge = get_task_engines()
    counts = {
        "requests_finished": 0,
        "keys_destroyed": 0,
        "financial_documents_purged": 0,
        "failed": 0,
    }
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
        # 3. Financial documents (#84; ADR-0001 amendment): business-keyed rows whose own CRA
        # clock has passed and whose linked client, if any, is not itself under a clinical
        # hold. Nothing is crypto-shredded here — the business key never shreds (ADR-0003 §5)
        # — row deletion is the erasure. The guard re-checks both conditions at DELETE time;
        # this query only proposes candidates.
        async with app.connect() as conn:
            financial = (
                await conn.execute(
                    text(
                        "SELECT d.id, d.kind FROM documents d "
                        "LEFT JOIN customers c ON c.id = d.linked_customer_id "
                        "WHERE d.key_owner = 'business' AND d.retain_until < now() "
                        "AND (d.linked_customer_id IS NULL OR "
                        "c.retention_expires_at IS NULL OR c.retention_expires_at < now())"
                    )
                )
            ).all()
        for document in financial:
            try:
                counts["financial_documents_purged"] += await _purge_document(
                    purge, str(document.id), document.kind
                )
            except Exception:
                counts["failed"] += 1
                log.exception("financial document %s could not be purged", document.id)
    finally:
        await app.dispose()
        await purge.dispose()
    log.info("purge_expired", extra=counts)
    await ping_maintenance()  # #85: dead-man ping, after the counts are final
    return counts


@celery_app.task(name="customers.tasks.finish_erasure")
def finish_erasure(request_id: str) -> bool:
    """Enqueued by `POST /customers/{id}/erasure` after its commit. A held client is left
    for `purge_expired`; a lost enqueue is picked up by it too."""
    return run_task(_finish_one, request_id)


@celery_app.task(name="customers.tasks.purge_expired")
def purge_expired() -> dict[str, int]:
    """Nightly from beat (`core/celery_app.py`). Safe to run by hand at any time — and it is
    the runbook's step after any database restore."""
    return run_task(_purge_expired)
