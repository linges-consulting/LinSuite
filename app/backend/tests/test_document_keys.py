"""Per-customer document keys under the deployment master key (Task 6, #41; ADR-0001 §5, D6).

S2: the wrap itself — the right master key and the right customer unwrap, anything else is a
GCM authentication failure, never bytes. S1: every client made through `create_customer`
(directly, or inline while booking) gets exactly one key; a client that predates the table
gets one lazily from `data_key`. S5: who may destroy a key — the app role never (grant *and*
trigger), the purge role only when the client's retention hold is null or expired.
"""

import asyncio
import base64
import os
import uuid

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from core import crypto
from core.config import get_settings
from core.db import get_purge_engine, session_scope
from customers import keys
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    CUSTOMERS,
    as_admin,
    at,
    book,
    claimed_instance,
    make_customer,
    make_service,
    me_staff_id,
    put_hours,
)

MASTER = "22" * 32
OTHER_MASTER = "33" * 32


# --- S2: wrap / unwrap ------------------------------------------------------------------------


def test_a_wrapped_key_unwraps_under_its_master_key_and_customer():
    customer_id, dek = uuid.uuid4(), os.urandom(32)

    wrapped = keys.wrap(dek, customer_id, MASTER)

    assert keys.unwrap(wrapped, customer_id, MASTER) == dek
    assert dek.hex() not in wrapped


def test_unwrapping_under_a_different_master_key_raises():
    customer_id = uuid.uuid4()
    wrapped = keys.wrap(os.urandom(32), customer_id, MASTER)

    with pytest.raises(InvalidTag):
        keys.unwrap(wrapped, customer_id, OTHER_MASTER)


def test_a_wrapped_key_copied_onto_another_customer_does_not_unwrap():
    wrapped = keys.wrap(os.urandom(32), uuid.uuid4(), MASTER)

    with pytest.raises(InvalidTag):
        keys.unwrap(wrapped, uuid.uuid4(), MASTER)


def test_a_tampered_wrapped_key_does_not_unwrap():
    customer_id = uuid.uuid4()
    raw = bytearray(base64.b64decode(keys.wrap(os.urandom(32), customer_id, MASTER)))
    raw[-1] ^= 1

    with pytest.raises(InvalidTag):
        keys.unwrap(base64.b64encode(bytes(raw)).decode(), customer_id, MASTER)


# --- S1: the creation path --------------------------------------------------------------------


async def key_rows(customer_id: str) -> int:
    async with session_scope() as db:
        return await db.scalar(
            text("SELECT count(*) FROM customer_document_keys WHERE customer_id = :id"),
            {"id": customer_id},
        )


async def data_key(customer_id: str) -> bytes:
    async with session_scope() as db:
        key = await keys.data_key(db, uuid.UUID(customer_id))
        await db.commit()
    return key


async def bare_customer(first_name: str = "Old") -> str:
    """A client inserted behind `create_customer`'s back — one that predates the table."""
    async with session_scope() as db:
        customer_id = await db.scalar(
            text("INSERT INTO customers (first_name, last_name) VALUES (:f, 'Timer') RETURNING id"),
            {"f": first_name},
        )
        await db.commit()
    return str(customer_id)


async def test_creating_a_customer_creates_exactly_one_key(client):
    await as_admin(client)

    customer_id = await make_customer(client)

    assert await key_rows(customer_id) == 1


async def test_a_customer_created_inline_while_booking_gets_a_key(client):
    await as_admin(client)
    me = await me_staff_id(client)
    await put_hours(client, me, [(0, 540, 720)])
    service = await make_service(client, [me])

    resp = await book(client, service, me, at("10:00"))

    assert resp.status_code == 201, resp.text
    assert await key_rows(resp.json()["customer"]["id"]) == 1


async def test_the_data_key_is_32_bytes_stable_usable_and_different_per_customer(client):
    await as_admin(client)
    first = await make_customer(client)
    second = (
        await client.post(CUSTOMERS, json={"first_name": "Sam", "last_name": "Okonkwo"})
    ).json()["id"]

    key = await data_key(first)

    assert len(key) == 32
    assert await data_key(first) == key
    assert await data_key(second) != key
    assert crypto.decrypt(crypto.encrypt("chart", key.hex()), key.hex()) == "chart"
    # Never stored as it is: the column holds the wrap, not the key.
    async with session_scope() as db:
        stored = await db.scalar(
            text("SELECT wrapped_key FROM customer_document_keys WHERE customer_id = :id"),
            {"id": first},
        )
    assert key.hex() not in stored
    assert keys.unwrap(stored, uuid.UUID(first), get_settings().document_master_key) == key


async def test_the_api_never_returns_a_key(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    key = await data_key(customer_id)
    async with session_scope() as db:
        wrapped = await db.scalar(
            text("SELECT wrapped_key FROM customer_document_keys WHERE customer_id = :id"),
            {"id": customer_id},
        )
    forms = (key.hex(), key.hex().upper(), base64.b64encode(key).decode(), wrapped)

    for resp in (
        await client.get(f"{CUSTOMERS}/{customer_id}"),
        await client.get(CUSTOMERS),
        await client.get(CUSTOMERS, params={"q": "nai"}),
    ):
        assert resp.status_code == 200, resp.text
        assert customer_id in resp.text  # the customer really was in the response
        for form in forms:
            assert form not in resp.text


async def test_a_customer_from_before_the_table_gets_a_key_on_first_use(client):
    customer_id = await bare_customer()
    assert await key_rows(customer_id) == 0

    key = await data_key(customer_id)

    assert await key_rows(customer_id) == 1
    assert await data_key(customer_id) == key


async def test_two_first_uses_at_once_make_one_key(client):
    customer_id = await bare_customer()

    first, second = await asyncio.gather(data_key(customer_id), data_key(customer_id))

    assert first == second
    assert await key_rows(customer_id) == 1


# --- S5: who may destroy a key ----------------------------------------------------------------


def sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


async def keyed_customer(retention_expires_at: str | None) -> str:
    customer_id = await bare_customer()
    await data_key(customer_id)
    async with session_scope() as db:
        await db.execute(
            text(
                "UPDATE customers SET retention_expires_at = "
                "cast(cast(:at AS text) AS timestamptz) WHERE id = :id"
            ),
            {"at": retention_expires_at, "id": customer_id},
        )
        await db.commit()
    return customer_id


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE customer_document_keys SET wrapped_key = 'x' WHERE customer_id = :id",
        "DELETE FROM customer_document_keys WHERE customer_id = :id",
    ],
)
async def test_the_app_role_holds_no_grant_to_rewrite_or_destroy_a_key(client, statement):
    customer_id = await keyed_customer(None)

    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(text(statement), {"id": customer_id})
        await db.rollback()

    assert sqlstate(refused.value) == "42501"
    assert await key_rows(customer_id) == 1


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE customer_document_keys SET wrapped_key = 'x' WHERE customer_id = :id",
        "DELETE FROM customer_document_keys WHERE customer_id = :id",
    ],
)
async def test_the_trigger_refuses_the_app_role_even_with_the_grant_restored(client, statement):
    """The grant restored in an owner transaction that is rolled back, so it is real for the
    one statement and never committed — the refusal is the trigger's alone."""
    customer_id = await keyed_customer(None)
    owner = create_async_engine(get_settings().database_url_migrate)
    try:
        async with owner.connect() as conn:
            await conn.begin()
            try:
                await conn.execute(
                    text("GRANT UPDATE, DELETE ON customer_document_keys TO linsuite_app")
                )
                await conn.execute(text("SET ROLE linsuite_app"))
                with pytest.raises(DBAPIError) as refused:
                    await conn.execute(text(statement), {"id": customer_id})
                assert sqlstate(refused.value) == "42501", refused.value
                assert "customer_document_keys" in str(refused.value)
            finally:
                await conn.rollback()
    finally:
        await owner.dispose()
    assert await key_rows(customer_id) == 1


async def purge_delete(customer_id: str) -> int:
    async with get_purge_engine().begin() as purge:
        result = await purge.execute(
            text("DELETE FROM customer_document_keys WHERE customer_id = :id"),
            {"id": customer_id},
        )
    return result.rowcount


@pytest.mark.parametrize("expires", [None, "2001-01-01T00:00:00Z"], ids=["not_held", "expired"])
async def test_the_purge_role_may_destroy_an_unheld_or_expired_key(client, expires):
    customer_id = await keyed_customer(expires)

    assert await purge_delete(customer_id) == 1
    assert await key_rows(customer_id) == 0


@pytest.mark.parametrize(
    "expires", ["2999-01-01T00:00:00Z", "infinity"], ids=["held_until", "held_indefinitely"]
)
async def test_the_purge_role_may_not_destroy_a_held_key(client, expires):
    customer_id = await keyed_customer(expires)

    with pytest.raises(DBAPIError) as refused:
        await purge_delete(customer_id)

    assert sqlstate(refused.value) == "42501"
    # The guard's own refusal, not a missing grant somewhere on the way to it.
    assert "customer_document_keys: DELETE is not permitted" in str(refused.value)
    assert await key_rows(customer_id) == 1


async def test_the_purge_role_can_write_the_fact_of_a_purge_into_audit_events(client):
    """ADR-0001 §6: the erasure and its audit row land in the one purge transaction."""
    async with get_purge_engine().connect() as purge:
        await purge.execute(
            text(
                "INSERT INTO audit_events (event_type, target_type) "
                "VALUES ('customer.key_destroyed', 'customer')"
            )
        )
        await purge.rollback()


# A trigger function runs with the caller's `search_path`, and `pg_temp` is searched before
# everything else — `pg_catalog` included — for any relation name it does not qualify. A
# role with TEMP (both have it) could shadow the table the guard reads and write its own
# answer. The guard pins `search_path = pg_catalog, pg_temp` and qualifies `public.customers`.
SHADOWS = {
    # "This customer is not held."
    "customers": (
        "CREATE TEMP TABLE customers (id uuid, retention_expires_at timestamptz)",
        "INSERT INTO customers VALUES (cast(:id AS uuid), NULL)",
    ),
    # "The purge role owns the table" — the owner branch lets everything through.
    "pg_class": (
        "CREATE TEMP TABLE pg_class (oid oid, relowner oid)",
        "INSERT INTO pg_class SELECT c.oid, r.oid FROM pg_catalog.pg_class c, "
        "pg_catalog.pg_roles r WHERE c.relname = 'customer_document_keys' "
        "AND r.rolname = 'linsuite_purge' AND cast(:id AS uuid) IS NOT NULL",
    ),
}


@pytest.mark.parametrize("shadowed", sorted(SHADOWS))
async def test_a_temp_table_cannot_talk_the_guard_into_destroying_a_held_key(client, shadowed):
    customer_id = await keyed_customer("infinity")
    create, fill = SHADOWS[shadowed]

    async with get_purge_engine().connect() as purge:
        await purge.begin()
        try:
            await purge.execute(text(create))
            await purge.execute(text(fill), {"id": customer_id})
            with pytest.raises(DBAPIError) as refused:
                await purge.execute(
                    text("DELETE FROM customer_document_keys WHERE customer_id = :id"),
                    {"id": customer_id},
                )
        finally:
            # The temp table goes with the transaction; the pooled connection stays clean.
            await purge.rollback()

    assert sqlstate(refused.value) == "42501"
    assert "customer_document_keys: DELETE is not permitted" in str(refused.value)
    assert await key_rows(customer_id) == 1
