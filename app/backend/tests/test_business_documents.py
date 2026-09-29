"""The business-owned document key tier (#55, ADR-0003).

S2: `billing.keys.wrap`/`unwrap` — the right master key opens, anything else is a GCM
authentication failure. `core.documents.store_document`/`fetch_document` refuse an
inconsistent `key_owner`/`customer_id`/`linked_customer_id` combination before touching the
database at all.
S1: a business-keyed document round-trips with no customer, and linked to one; it survives
that customer's own crypto-shred untouched, even when linked to them; `retain_until` round
trips independently of the customer's clinical hold.
S5: who may change or remove the business key — nobody but the table owner, ever (there is no
purge-eligible branch, unlike `customer_document_keys`); the database's own CHECK constraints
on `documents`, not just this module's validation.
S5/purge task (#84; ADR-0001 amendment): the guard's business-keyed branch — refused before
`retain_until`, refused while a linked client is held (dated or `'infinity'`), permitted once
both clear, always refused to the app role — and `customers.tasks._purge_expired`'s third
step, which audits and deletes eligible rows, idempotently.
"""

import asyncio
import base64
import os
import uuid
from datetime import UTC, datetime

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from billing import keys as billing_keys
from core import documents
from core.config import get_settings
from core.db import get_purge_engine, session_scope
from core.errors import DocumentNotFound
from customers import keys as customer_keys
from customers import tasks
from tests.test_appointments import claimed_instance  # noqa: F401 — the autouse fixture comes along
from tests.test_document_keys import keyed_customer, sqlstate

PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ninvoice\n%%EOF"
MASTER = "22" * 32
OTHER_MASTER = "33" * 32


# --- S2: wrap / unwrap -----------------------------------------------------------------------


def test_the_business_key_wraps_and_unwraps_under_its_master_key():
    dek = os.urandom(32)

    wrapped = billing_keys.wrap(dek, MASTER)

    assert billing_keys.unwrap(wrapped, MASTER) == dek
    assert dek.hex() not in wrapped


def test_unwrapping_the_business_key_under_a_different_master_key_raises():
    wrapped = billing_keys.wrap(os.urandom(32), MASTER)

    with pytest.raises(InvalidTag):
        billing_keys.unwrap(wrapped, OTHER_MASTER)


def test_a_tampered_wrapped_business_key_does_not_unwrap():
    raw = bytearray(base64.b64decode(billing_keys.wrap(os.urandom(32), MASTER)))
    raw[-1] ^= 1

    with pytest.raises(InvalidTag):
        billing_keys.unwrap(base64.b64encode(bytes(raw)).decode(), MASTER)


# --- S2: the explicit owner parameter, checked before any database access --------------------


async def test_store_document_requires_customer_id_for_the_customer_tier():
    with pytest.raises(ValueError):
        await documents.store_document(
            None,
            key=b"0" * 32,
            key_owner="customer",
            kind="probe",
            source_id=uuid.uuid4(),
            content=b"x",
            content_type="text/plain",
        )


async def test_store_document_refuses_a_customer_id_for_the_business_tier():
    with pytest.raises(ValueError):
        await documents.store_document(
            None,
            key=b"0" * 32,
            key_owner="business",
            customer_id=uuid.uuid4(),
            kind="probe",
            source_id=uuid.uuid4(),
            content=b"x",
            content_type="text/plain",
        )


async def test_store_document_refuses_a_linked_customer_id_for_the_customer_tier():
    with pytest.raises(ValueError):
        await documents.store_document(
            None,
            key=b"0" * 32,
            key_owner="customer",
            customer_id=uuid.uuid4(),
            linked_customer_id=uuid.uuid4(),
            kind="probe",
            source_id=uuid.uuid4(),
            content=b"x",
            content_type="text/plain",
        )


async def test_fetch_document_requires_customer_id_for_the_customer_tier():
    with pytest.raises(ValueError):
        await documents.fetch_document(
            None, key=b"0" * 32, key_owner="customer", document_id=uuid.uuid4()
        )


async def test_fetch_document_refuses_a_customer_id_for_the_business_tier():
    with pytest.raises(ValueError):
        await documents.fetch_document(
            None,
            key=b"0" * 32,
            key_owner="business",
            customer_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
        )


# --- helpers ------------------------------------------------------------------------------


async def business_key_row_exists() -> bool:
    async with session_scope() as db:
        return bool(
            await db.scalar(
                text("SELECT count(*) FROM business_document_keys WHERE business_id = 1")
            )
        )


async def store_business(
    *,
    linked_customer_id: str | None = None,
    retain_until: datetime | None = None,
    content: bytes = PDF,
    source_id: uuid.UUID | None = None,
) -> uuid.UUID:
    async with session_scope() as db:
        key = await billing_keys.business_key(db)
        document_id = await documents.store_document(
            db,
            key=key,
            key_owner="business",
            linked_customer_id=uuid.UUID(linked_customer_id) if linked_customer_id else None,
            retain_until=retain_until,
            kind="invoice",
            source_id=source_id or uuid.uuid4(),
            content=content,
            content_type="application/pdf",
        )
        await db.commit()
    return document_id


async def fetch_business(document_id: uuid.UUID) -> tuple[bytes, str]:
    async with session_scope() as db:
        key = await billing_keys.business_key(db)
        return await documents.fetch_document(
            db, key=key, key_owner="business", document_id=document_id
        )


async def stored(document_id: uuid.UUID) -> dict:
    async with session_scope() as db:
        found = await db.execute(
            text("SELECT * FROM documents WHERE id = :id"), {"id": document_id}
        )
        return dict(found.one()._mapping)


# --- S1: the key itself ------------------------------------------------------------------------


async def test_the_business_key_is_created_lazily_and_is_stable(client):
    async with session_scope() as db:
        first = await billing_keys.business_key(db)
        await db.commit()

    async with session_scope() as db:
        second = await billing_keys.business_key(db)

    assert len(first) == 32
    assert first == second
    assert await business_key_row_exists()


# --- S1: store / fetch ---------------------------------------------------------------------


async def test_a_business_keyed_document_round_trips_with_no_customer_at_all(client):
    """The anonymous retail sale (#55's own example)."""
    document_id = await store_business()

    assert await fetch_business(document_id) == (PDF, "application/pdf")
    row = await stored(document_id)
    assert row["key_owner"] == "business"
    assert row["customer_id"] is None
    assert row["linked_customer_id"] is None


async def test_a_business_keyed_document_round_trips_linked_to_a_customer(client):
    customer_id = await keyed_customer(None)

    document_id = await store_business(linked_customer_id=customer_id)

    assert await fetch_business(document_id) == (PDF, "application/pdf")
    row = await stored(document_id)
    assert row["customer_id"] is None
    assert str(row["linked_customer_id"]) == customer_id


async def test_a_business_keyed_document_is_not_reachable_through_the_customer_tier(client):
    customer_id = await keyed_customer(None)
    document_id = await store_business(linked_customer_id=customer_id)

    async with session_scope() as db:
        key = await customer_keys.existing_key(db, uuid.UUID(customer_id))
        with pytest.raises(DocumentNotFound):
            await documents.fetch_document(
                db,
                key=key,
                key_owner="customer",
                customer_id=uuid.UUID(customer_id),
                document_id=document_id,
            )


async def test_retain_until_round_trips(client):
    expiry = datetime(2032, 1, 1, tzinfo=UTC)

    document_id = await store_business(retain_until=expiry)

    row = await stored(document_id)
    assert row["retain_until"] == expiry


# --- S1: the acceptance criterion — a customer's crypto-shred never touches one ----------------


async def test_shredding_a_customers_key_never_touches_a_business_keyed_document_linked_to_them(
    client,
):
    customer_id = await keyed_customer(None)  # not held: eligible to shred right away
    expiry = datetime(2032, 1, 1, tzinfo=UTC)
    document_id = await store_business(linked_customer_id=customer_id, retain_until=expiry)

    assert await tasks._shred(get_purge_engine(), customer_id, None) is True

    # the client's own key really is gone
    async with session_scope() as db:
        assert await customer_keys.existing_key(db, uuid.UUID(customer_id)) is None
        await db.commit()
    # the business-keyed document is untouched: still there, still readable, still linked,
    # its own retention clock unchanged
    assert await fetch_business(document_id) == (PDF, "application/pdf")
    row = await stored(document_id)
    assert str(row["linked_customer_id"]) == customer_id
    assert row["retain_until"] == expiry


async def test_a_business_keyed_document_is_never_purge_eligible_from_an_expired_clinical_hold(
    client,
):
    """#55's financial-retention criterion: a linked customer's clinical hold expiring (and
    that key actually being destroyed) purges nothing on the financial side."""
    customer_id = await keyed_customer("2001-01-01T00:00:00Z")  # already expired
    document_id = await store_business(linked_customer_id=customer_id)

    shredded = await tasks._shred(get_purge_engine(), customer_id, None)

    assert shredded is True
    assert await fetch_business(document_id) == (PDF, "application/pdf")


# --- S5: the database's own CHECK constraints, not just this module's validation --------------


async def _insert_raw(sql: str, **params) -> None:
    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(text(sql), params)
        await db.rollback()
    assert sqlstate(refused.value) == "23514", refused.value


async def test_the_database_refuses_a_business_row_with_a_customer_id(client):
    await _insert_raw(
        "INSERT INTO documents (customer_id, key_owner, kind, source_id, content_type, "
        "ciphertext, digest, size_bytes) VALUES "
        "(gen_random_uuid(), 'business', 'probe', gen_random_uuid(), 'application/pdf', "
        "'\\x00'::bytea, decode(repeat('00', 64), 'hex'), 1)"
    )


async def test_the_database_refuses_a_customer_row_with_no_customer_id(client):
    await _insert_raw(
        "INSERT INTO documents (customer_id, key_owner, kind, source_id, content_type, "
        "ciphertext, digest, size_bytes) VALUES "
        "(NULL, 'customer', 'probe', gen_random_uuid(), 'application/pdf', "
        "'\\x00'::bytea, decode(repeat('00', 64), 'hex'), 1)"
    )


async def test_the_database_refuses_a_customer_row_with_a_linked_customer_id(client):
    customer_id = await keyed_customer(None)
    await _insert_raw(
        "INSERT INTO documents (customer_id, key_owner, linked_customer_id, kind, source_id, "
        "content_type, ciphertext, digest, size_bytes) VALUES "
        "(:c, 'customer', :c, 'probe', gen_random_uuid(), 'application/pdf', "
        "'\\x00'::bytea, decode(repeat('00', 64), 'hex'), 1)",
        c=customer_id,
    )


# --- S5: who may change or remove the business key ---------------------------------------------

REWRITES = [
    "UPDATE business_document_keys SET wrapped_key = 'x' WHERE business_id = 1",
    "DELETE FROM business_document_keys WHERE business_id = 1",
]


@pytest.mark.parametrize("statement", REWRITES)
async def test_the_app_role_holds_no_grant_to_rewrite_or_destroy_the_business_key(
    client, statement
):
    async with session_scope() as db:
        await billing_keys.create_key(db)
        await db.commit()

    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(text(statement))
        await db.rollback()

    assert sqlstate(refused.value) == "42501"
    assert await business_key_row_exists()


@pytest.mark.parametrize("statement", REWRITES)
async def test_the_guard_refuses_the_app_role_even_with_the_grant_restored(client, statement):
    async with session_scope() as db:
        await billing_keys.create_key(db)
        await db.commit()

    owner = create_async_engine(get_settings().database_url_migrate)
    try:
        async with owner.connect() as conn:
            await conn.begin()
            try:
                await conn.execute(
                    text("GRANT UPDATE, DELETE ON business_document_keys TO linsuite_app")
                )
                await conn.execute(text("SET ROLE linsuite_app"))
                with pytest.raises(DBAPIError) as refused:
                    await conn.execute(text(statement))
                assert sqlstate(refused.value) == "42501", refused.value
                assert "business_document_keys" in str(refused.value)
            finally:
                await conn.rollback()
    finally:
        await owner.dispose()

    assert await business_key_row_exists()


async def test_the_purge_role_may_not_destroy_the_business_key_even_with_the_grant_restored(
    client,
):
    """Unlike `customer_document_keys`, there is no purge-eligible branch: this key is never
    destroyed in v1, so even the purge role handed DELETE back is refused."""
    async with session_scope() as db:
        await billing_keys.create_key(db)
        await db.commit()

    owner = create_async_engine(get_settings().database_url_migrate)
    try:
        async with owner.connect() as conn:
            await conn.begin()
            try:
                await conn.execute(text("GRANT DELETE ON business_document_keys TO linsuite_purge"))
                await conn.execute(text("SET ROLE linsuite_purge"))
                with pytest.raises(DBAPIError) as refused:
                    await conn.execute(
                        text("DELETE FROM business_document_keys WHERE business_id = 1")
                    )
                assert sqlstate(refused.value) == "42501", refused.value
            finally:
                await conn.rollback()
    finally:
        await owner.dispose()

    assert await business_key_row_exists()


async def _business_key() -> bytes:
    async with session_scope() as db:
        key = await billing_keys.business_key(db)
        await db.commit()
    return key


async def test_two_first_uses_at_once_make_one_business_key(client):
    first, second = await asyncio.gather(_business_key(), _business_key())

    assert first == second
    async with session_scope() as db:
        count = await db.scalar(text("SELECT count(*) FROM business_document_keys"))
    assert count == 1


# --- S5: the financial purge guard (#84; ADR-0001 amendment) ---------------------------------

PAST = datetime(2001, 1, 1, tzinfo=UTC)
FUTURE = datetime(2999, 1, 1, tzinfo=UTC)


async def purge_delete_document(document_id: uuid.UUID) -> int:
    async with get_purge_engine().begin() as purge:
        result = await purge.execute(
            text("DELETE FROM documents WHERE id = :id"), {"id": document_id}
        )
    return result.rowcount


async def test_the_purge_role_is_refused_a_business_document_before_retain_until(client):
    document_id = await store_business(retain_until=FUTURE)

    with pytest.raises(DBAPIError) as refused:
        await purge_delete_document(document_id)

    assert sqlstate(refused.value) == "42501"
    assert "documents: DELETE is not permitted" in str(refused.value)
    assert (await stored(document_id))["id"] == document_id


@pytest.mark.parametrize(
    "hold", ["2999-01-01T00:00:00Z", "infinity"], ids=["held_until", "held_indefinitely"]
)
async def test_the_purge_role_is_refused_a_business_document_whose_linked_client_is_held(
    client, hold
):
    customer_id = await keyed_customer(hold)
    document_id = await store_business(linked_customer_id=customer_id, retain_until=PAST)

    with pytest.raises(DBAPIError) as refused:
        await purge_delete_document(document_id)

    assert sqlstate(refused.value) == "42501"
    assert "documents: DELETE is not permitted" in str(refused.value)
    assert (await stored(document_id))["id"] == document_id


async def test_the_purge_role_may_delete_an_expired_business_document_with_no_linked_client(
    client,
):
    document_id = await store_business(retain_until=PAST)

    assert await purge_delete_document(document_id) == 1


@pytest.mark.parametrize("hold", [None, "2001-01-01T00:00:00Z"], ids=["not_held", "expired"])
async def test_the_purge_role_may_delete_an_expired_business_document_with_an_unheld_client(
    client, hold
):
    customer_id = await keyed_customer(hold)
    document_id = await store_business(linked_customer_id=customer_id, retain_until=PAST)

    assert await purge_delete_document(document_id) == 1


async def test_the_app_role_is_always_refused_an_expired_business_document(client):
    document_id = await store_business(retain_until=PAST)

    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(text("DELETE FROM documents WHERE id = :id"), {"id": document_id})
        await db.rollback()

    assert sqlstate(refused.value) == "42501"
    assert (await stored(document_id))["id"] == document_id


# --- purge task: financial documents (#84) ----------------------------------------------------


async def purged_document_events(document_id: uuid.UUID) -> list[dict]:
    async with get_purge_engine().connect() as purge:
        rows = await purge.execute(
            text(
                "SELECT target_id, metadata FROM audit_events "
                "WHERE event_type = 'document.purged' AND target_id = :d ORDER BY id"
            ),
            {"d": str(document_id)},
        )
        return [dict(r._mapping) for r in rows]


async def document_exists(document_id: uuid.UUID) -> bool:
    async with session_scope() as db:
        return bool(
            await db.scalar(
                text("SELECT count(*) FROM documents WHERE id = :id"), {"id": document_id}
            )
        )


async def test_the_nightly_purge_deletes_an_expired_anonymous_document_and_audits_once(client):
    document_id = await store_business(retain_until=PAST)

    counts = await tasks._purge_expired()

    assert counts["financial_documents_purged"] == 1
    assert counts["failed"] == 0
    assert await document_exists(document_id) is False
    events = await purged_document_events(document_id)
    assert len(events) == 1
    assert events[0]["target_id"] == str(document_id)
    # No content, no names: exactly the three identifiers the ticket calls for.
    assert set(events[0]["metadata"]) == {"authority", "kind", "document_id"}
    assert events[0]["metadata"] == {
        "authority": "linsuite_purge",
        "kind": "invoice",
        "document_id": str(document_id),
    }


async def test_the_nightly_purge_leaves_a_held_clients_expired_document_untouched(client):
    customer_id = await keyed_customer("2999-01-01T00:00:00Z")
    document_id = await store_business(linked_customer_id=customer_id, retain_until=PAST)

    counts = await tasks._purge_expired()

    assert counts["financial_documents_purged"] == 0
    assert counts["failed"] == 0
    assert await document_exists(document_id) is True
    assert await purged_document_events(document_id) == []


async def test_a_second_purge_run_deletes_nothing_and_audits_nothing_new(client):
    document_id = await store_business(retain_until=PAST)
    first = await tasks._purge_expired()
    assert first["financial_documents_purged"] == 1

    second = await tasks._purge_expired()

    assert second["financial_documents_purged"] == 0
    assert second["failed"] == 0
    assert len(await purged_document_events(document_id)) == 1


async def test_customer_keyed_purging_is_unaffected_by_the_financial_purge_step(client):
    """The pre-existing key shred still runs, and counts its own key, not a document."""
    await keyed_customer(PAST.isoformat())  # keyed_customer already keys it

    counts = await tasks._purge_expired()

    assert counts["keys_destroyed"] == 1
    assert counts["financial_documents_purged"] == 0
