"""The document store (#44): `core.crypto.seal`, `core.documents`, the `documents` table, and
the purge path that destroys a client's documents before their key.

S2: `seal`/`open_sealed` — the right key and associated data open, anything else raises.
S1: `store_document` → `fetch_document` round-trips under the client's DEK, and every way a
stored row can be altered (blob, hash, a blob copied onto another row) fails verification.
S5: who may change or remove a document — the app role never (grant *and* trigger), the
purge role only DELETE and only while the client is not held; the FK to the key row.
S1: `customers.tasks._shred` deletes documents, then the key, or — held — nothing.
"""

import os
import uuid

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from core import crypto, documents
from core.config import get_settings
from core.db import get_purge_engine, session_scope
from core.errors import DocumentIntegrityError
from customers import keys, tasks
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    as_admin,
    claimed_instance,
)
from tests.test_customer_profile import ready_customer
from tests.test_document_keys import key_rows, keyed_customer, sqlstate
from tests.test_retention_api import switch

PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\nsigned consent: Priya Nair\n%%EOF"


# --- S2: seal / open_sealed -------------------------------------------------------------------


def test_a_sealed_blob_opens_under_its_key_and_associated_data():
    key, ad = os.urandom(32), uuid.uuid4().bytes

    blob = crypto.seal(PDF, key, ad)

    assert crypto.open_sealed(blob, key, ad) == PDF
    assert PDF not in blob
    assert len(blob) == 12 + len(PDF) + 16  # nonce || ciphertext || tag


def test_two_seals_of_the_same_bytes_differ():
    key, ad = os.urandom(32), b"doc"

    assert crypto.seal(PDF, key, ad) != crypto.seal(PDF, key, ad)


def test_a_flipped_byte_a_wrong_key_or_other_associated_data_raises():
    key, ad = os.urandom(32), b"doc"
    blob = bytearray(crypto.seal(PDF, key, ad))
    tampered = bytes(blob[:-1]) + bytes([blob[-1] ^ 1])
    in_the_body = bytes(blob[:20]) + bytes([blob[20] ^ 1]) + bytes(blob[21:])

    for attempt in (
        lambda: crypto.open_sealed(tampered, key, ad),
        lambda: crypto.open_sealed(in_the_body, key, ad),
        lambda: crypto.open_sealed(bytes(blob), os.urandom(32), ad),
        lambda: crypto.open_sealed(bytes(blob), key, b"other"),
    ):
        with pytest.raises(InvalidTag):
            attempt()


def test_a_key_that_is_not_aes_256_is_refused():
    with pytest.raises(ValueError):
        crypto.seal(PDF, os.urandom(16), b"doc")


# --- helpers ----------------------------------------------------------------------------------


async def store(
    customer_id: str, content: bytes = PDF, *, source_id: uuid.UUID | None = None
) -> uuid.UUID:
    async with session_scope() as db:
        key = await keys.data_key(db, uuid.UUID(customer_id))
        document_id = await documents.store_document(
            db,
            key=key,
            customer_id=uuid.UUID(customer_id),
            kind="form_submission",
            source_id=source_id or uuid.uuid4(),
            content=content,
            content_type="application/pdf",
        )
        await db.commit()
    return document_id


async def fetch(customer_id: str, document_id: uuid.UUID) -> tuple[bytes, str]:
    async with session_scope() as db:
        key = await keys.existing_key(db, uuid.UUID(customer_id))
        assert key is not None
        return await documents.fetch_document(db, key=key, document_id=document_id)


async def stored(document_id: uuid.UUID) -> dict:
    async with session_scope() as db:
        found = await db.execute(
            text("SELECT * FROM documents WHERE id = :id"), {"id": document_id}
        )
        return dict(found.one()._mapping)


async def document_rows(customer_id: str) -> int:
    async with session_scope() as db:
        return await db.scalar(
            text("SELECT count(*) FROM documents WHERE customer_id = :id"), {"id": customer_id}
        )


async def as_owner(statement: str, **params) -> None:
    owner = create_async_engine(get_settings().database_url_migrate)
    try:
        async with owner.begin() as conn:
            await conn.execute(text(statement), params)
    finally:
        await owner.dispose()


async def key_destroyed_events(customer_id: str) -> int:
    async with get_purge_engine().connect() as purge:
        return await purge.scalar(
            text(
                "SELECT count(*) FROM audit_events "
                "WHERE event_type = 'customer.key_destroyed' AND target_id = :c"
            ),
            {"c": customer_id},
        )


# --- S1: store / fetch ------------------------------------------------------------------------


async def test_a_stored_document_round_trips_and_its_hash_verifies(client):
    customer_id = await keyed_customer(None)

    document_id = await store(customer_id)

    assert await fetch(customer_id, document_id) == (PDF, "application/pdf")
    row = await stored(document_id)
    assert PDF not in row["ciphertext"]
    assert b"Priya" not in row["ciphertext"]
    assert row["size_bytes"] == len(PDF)
    assert len(row["sha256"]) == 32
    assert str(row["customer_id"]) == customer_id


async def test_two_stores_of_the_same_bytes_produce_different_ciphertexts(client):
    customer_id = await keyed_customer(None)

    first, second = await store(customer_id), await store(customer_id)

    assert (await stored(first))["ciphertext"] != (await stored(second))["ciphertext"]


async def test_storing_the_same_source_twice_keeps_one_row(client):
    """A re-run render: `(kind, source_id)` is unique and the second insert does nothing."""
    customer_id, source = await keyed_customer(None), uuid.uuid4()

    first = await store(customer_id, source_id=source)
    again = await store(customer_id, b"a different render", source_id=source)

    assert again == first
    assert await document_rows(customer_id) == 1
    assert (await fetch(customer_id, first))[0] == PDF


def flip_last_byte(column: str) -> str:
    return (
        f"UPDATE documents SET {column} = overlay({column} placing "
        f"set_byte(substring({column} from octet_length({column}) for 1), 0, "
        f"get_byte({column}, octet_length({column}) - 1) # 1) "
        f"from octet_length({column}) for 1) WHERE id = :id"
    )


@pytest.mark.parametrize("column", ["ciphertext", "sha256"])
async def test_a_tampered_blob_or_hash_fails_verification(client, column, caplog):
    customer_id = await keyed_customer(None)
    document_id = await store(customer_id)
    await as_owner(flip_last_byte(column), id=document_id)

    with pytest.raises(DocumentIntegrityError):
        await fetch(customer_id, document_id)

    # Logged with the id, never the content.
    assert str(document_id) in caplog.text
    assert "Priya" not in caplog.text


async def test_a_blob_copied_onto_another_document_row_fails_verification(client):
    customer_id = await keyed_customer(None)
    source, target = await store(customer_id), await store(customer_id, b"another form")
    await as_owner(
        "UPDATE documents SET ciphertext = (SELECT ciphertext FROM documents WHERE id = :s), "
        "sha256 = (SELECT sha256 FROM documents WHERE id = :s) WHERE id = :t",
        s=source,
        t=target,
    )

    with pytest.raises(DocumentIntegrityError):
        await fetch(customer_id, target)


async def test_a_document_moved_onto_another_client_fails_verification(client):
    """The customer id is in the associated data too: re-pointing a row at another client
    (and handing it that client's key) does not open it."""
    owner_id, other_id = await keyed_customer(None), await keyed_customer(None)
    document_id = await store(owner_id)
    await as_owner(
        "UPDATE documents SET customer_id = :o WHERE id = :id", o=other_id, id=document_id
    )

    with pytest.raises(DocumentIntegrityError):
        await fetch(other_id, document_id)


async def test_existing_key_never_creates_one(client):
    customer_id = await keyed_customer(None)
    await as_owner("DELETE FROM customer_document_keys WHERE customer_id = :id", id=customer_id)

    async with session_scope() as db:
        assert await keys.existing_key(db, uuid.UUID(customer_id)) is None
        await db.commit()

    assert await key_rows(customer_id) == 0


async def test_existing_key_is_the_data_key(client):
    customer_id = await keyed_customer(None)

    async with session_scope() as db:
        assert await keys.existing_key(db, uuid.UUID(customer_id)) == await keys.data_key(
            db, uuid.UUID(customer_id)
        )


# --- S5: grants, the guard, the FK ------------------------------------------------------------

REWRITES = [
    "UPDATE documents SET content_type = 'text/plain' WHERE id = :id",
    "DELETE FROM documents WHERE id = :id",
]


@pytest.mark.parametrize("statement", REWRITES)
async def test_the_app_role_holds_no_grant_to_rewrite_or_delete_a_document(client, statement):
    customer_id = await keyed_customer(None)
    document_id = await store(customer_id)

    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(text(statement), {"id": document_id})
        await db.rollback()

    assert sqlstate(refused.value) == "42501"
    assert await document_rows(customer_id) == 1


async def refused_with_grant_restored(role: str, statement: str, document_id) -> DBAPIError:
    """The grant restored in an owner transaction that is rolled back — real for the one
    statement, never committed — so the refusal is the trigger's alone."""
    owner = create_async_engine(get_settings().database_url_migrate)
    try:
        async with owner.connect() as conn:
            await conn.begin()
            try:
                await conn.execute(text(f"GRANT UPDATE, DELETE ON documents TO {role}"))
                await conn.execute(text(f"SET ROLE {role}"))
                with pytest.raises(DBAPIError) as refused:
                    await conn.execute(text(statement), {"id": document_id})
            finally:
                await conn.rollback()
    finally:
        await owner.dispose()
    return refused.value


@pytest.mark.parametrize("statement", REWRITES)
async def test_the_guard_refuses_the_app_role_even_with_the_grant_restored(client, statement):
    customer_id = await keyed_customer(None)
    document_id = await store(customer_id)

    error = await refused_with_grant_restored("linsuite_app", statement, document_id)

    assert sqlstate(error) == "42501", error
    assert "documents: " in str(error)
    assert await document_rows(customer_id) == 1


async def test_the_guard_refuses_the_purge_role_an_update_even_unheld(client):
    customer_id = await keyed_customer(None)
    document_id = await store(customer_id)

    error = await refused_with_grant_restored("linsuite_purge", REWRITES[0], document_id)

    assert sqlstate(error) == "42501", error
    assert "documents: UPDATE is not permitted" in str(error)


async def purge_delete(customer_id: str) -> int:
    async with get_purge_engine().begin() as purge:
        result = await purge.execute(
            text("DELETE FROM documents WHERE customer_id = :id"), {"id": customer_id}
        )
    return result.rowcount


@pytest.mark.parametrize("expires", [None, "2001-01-01T00:00:00Z"], ids=["not_held", "expired"])
async def test_the_purge_role_may_delete_an_unheld_clients_documents(client, expires):
    customer_id = await keyed_customer(expires)
    await store(customer_id)

    assert await purge_delete(customer_id) == 1
    assert await document_rows(customer_id) == 0


@pytest.mark.parametrize(
    "expires", ["2999-01-01T00:00:00Z", "infinity"], ids=["held_until", "held_indefinitely"]
)
async def test_the_purge_role_may_not_delete_a_held_clients_documents(client, expires):
    customer_id = await keyed_customer(expires)
    await store(customer_id)

    with pytest.raises(DBAPIError) as refused:
        await purge_delete(customer_id)

    assert sqlstate(refused.value) == "42501"
    assert "documents: DELETE is not permitted" in str(refused.value)
    assert await document_rows(customer_id) == 1


async def test_a_temp_customers_table_cannot_make_a_held_clients_document_deletable(client):
    customer_id = await keyed_customer("infinity")
    await store(customer_id)

    async with get_purge_engine().connect() as purge:
        await purge.begin()
        try:
            await purge.execute(
                text("CREATE TEMP TABLE customers (id uuid, retention_expires_at timestamptz)")
            )
            await purge.execute(
                text("INSERT INTO customers VALUES (cast(:id AS uuid), NULL)"), {"id": customer_id}
            )
            with pytest.raises(DBAPIError) as refused:
                await purge.execute(
                    text("DELETE FROM documents WHERE customer_id = :id"), {"id": customer_id}
                )
        finally:
            await purge.rollback()

    assert "documents: DELETE is not permitted" in str(refused.value)
    assert await document_rows(customer_id) == 1


async def test_a_key_with_documents_cannot_be_deleted_even_by_the_owner(client):
    """No cascade (ADR-0001 rule 7): the FK is what serialises a purge against an insert."""
    customer_id = await keyed_customer(None)
    await store(customer_id)

    with pytest.raises(DBAPIError) as refused:
        await as_owner("DELETE FROM customer_document_keys WHERE customer_id = :id", id=customer_id)

    assert sqlstate(refused.value) == "23503"
    assert await key_rows(customer_id) == 1


# --- S1: the shred hook -----------------------------------------------------------------------


async def test_shredding_an_unheld_client_deletes_documents_then_the_key(client):
    customer_id = await keyed_customer(None)
    await store(customer_id)
    await store(customer_id, b"second form")

    assert await tasks._shred(get_purge_engine(), customer_id, None) is True

    assert await document_rows(customer_id) == 0
    assert await key_rows(customer_id) == 0
    assert await key_destroyed_events(customer_id) == 1
    async with session_scope() as db:
        assert await keys.existing_key(db, uuid.UUID(customer_id)) is None
        await db.commit()
    assert await key_rows(customer_id) == 0


@pytest.mark.parametrize(
    "expires", ["2999-01-01T00:00:00Z", "infinity"], ids=["held_until", "held_indefinitely"]
)
async def test_shredding_a_held_client_deletes_nothing_and_records_nothing(client, expires):
    customer_id = await keyed_customer(expires)
    await store(customer_id)

    assert await tasks._shred(get_purge_engine(), customer_id, None) is False

    assert await document_rows(customer_id) == 1
    assert await key_rows(customer_id) == 1
    assert await key_destroyed_events(customer_id) == 0


async def test_an_unheld_erasure_through_the_api_destroys_the_documents(client):
    await as_admin(client)
    await switch(client, "general_business")
    customer_id, _, _ = await ready_customer(client)
    await store(customer_id)

    resp = await client.post(f"/api/customers/{customer_id}/erasure", json={})

    assert resp.status_code == 201, resp.text
    assert resp.json()["purged_at"] is not None
    assert await document_rows(customer_id) == 0
    assert await key_rows(customer_id) == 0


async def test_the_nightly_job_destroys_an_expired_clients_documents_with_the_key(client):
    """Expired hold, no request: key shredded, profile kept — and the FK means the documents
    have to go first, in the same transaction."""
    customer_id = await keyed_customer("2001-01-01T00:00:00Z")
    await store(customer_id)

    tasks.purge_expired.delay()

    assert await document_rows(customer_id) == 0
    assert await key_rows(customer_id) == 0
    assert await key_destroyed_events(customer_id) == 1
