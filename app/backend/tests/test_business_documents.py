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
