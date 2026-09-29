"""S1: stock movements — receiving a delivery, correcting a count, and the two independent
permissions that gate them (#61).

`inventory.receive` and `inventory.adjust` are checked server-side, independently, on their
own routes; both default to Administrator only. `inventory/stock.py::record_movement` is the
one writer of `product_variants.quantity_on_hand`, and its single atomic
`UPDATE ... WHERE quantity_on_hand + :delta >= 0` — not an app-level lock — is what the
concurrency test below exercises for real, the same interleaving
`test_appointments.py::test_two_concurrent_bookings_of_one_slot_yield_one_201_and_one_409`
forces with a monkeypatched delay.
"""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db import get_purge_engine, session_scope
from tests.test_inventory import as_admin, make_product, make_variant

TABLE = "stock_movements"
TRIGGER = "stock_movements_no_rewrite"

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": EMAIL,
    "admin_password": PASSWORD,
}

PRODUCTS = "/api/admin/products"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
        # Append-only: the app role holds no DELETE on this table (the migration's own
        # grant/trigger pair), so resetting it between tests goes through the purge role,
        # the same way `test_inventory.py`'s own fixture already resets `audit_events`.
        await purge.execute(text(f"DELETE FROM {TABLE}"))
    async with session_scope() as db:
        for table in (
            "product_variants",
            "products",
            "password_reset_tokens",
            "users",
            "businesses",
            "setup_token",
        ):
            await db.execute(text(f"DELETE FROM {table}"))
        await db.execute(text("DELETE FROM roles WHERE NOT is_system"))
        await db.commit()

    from auth import setup

    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET mfa_required_for_admin = false"))
        await db.commit()
    client.cookies.clear()
    yield


# --- helpers --------------------------------------------------------------------------------


async def movements(variant_id: str) -> list[dict]:
    async with session_scope() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT kind, quantity_delta, reason, actor_user_id FROM stock_movements "
                        "WHERE variant_id = :v ORDER BY created_at"
                    ),
                    {"v": variant_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def quantity_on_hand(variant_id: str) -> int:
    async with session_scope() as db:
        return await db.scalar(
            text("SELECT quantity_on_hand FROM product_variants WHERE id = :id"),
            {"id": variant_id},
        )


async def new_variant(client, **overrides) -> dict:
    product = await make_variant(client, (await make_product(client))["id"], **overrides)
    return product["variants"][0]


def receive_url(variant: dict) -> str:
    return f"{PRODUCTS}/{variant['product_id']}/variants/{variant['id']}/receive"


def adjust_url(variant: dict) -> str:
    return f"{PRODUCTS}/{variant['product_id']}/variants/{variant['id']}/adjust"


async def deputy_with(client, capabilities: list[str]) -> tuple[str, str]:
    """A second admin-mode-capable account holding `admin` plus `capabilities`. Returns
    `(email, password)`; the caller still has to sign in as them."""
    from core.security import hash_password
    from tests.conftest import add_account

    role = await client.post(
        "/api/admin/roles",
        json={
            "name": "Deputy",
            "description": "Nearly.",
            "capabilities": ["admin", *capabilities],
        },
    )
    assert role.status_code == 201, role.text
    password = "correct horse battery 2"
    await add_account("deputy@cedar.example", await hash_password(password), role=role.json()["id"])
    return "deputy@cedar.example", password


# --- receiving --------------------------------------------------------------------------------


async def test_receiving_stock_increases_the_count_and_writes_a_movement(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)

    resp = await client.post(receive_url(variant), json={"quantity": 5, "reason": "PO 1042"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["variants"][0]["quantity_on_hand"] == 15
    rows = await movements(variant["id"])
    assert len(rows) == 1
    assert rows[0]["kind"] == "receipt"
    assert rows[0]["quantity_delta"] == 5
    assert rows[0]["reason"] == "PO 1042"
    assert rows[0]["actor_user_id"] is not None


async def test_receiving_stock_needs_no_reason(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)

    resp = await client.post(receive_url(variant), json={"quantity": 5})

    assert resp.status_code == 200, resp.text
    assert (await movements(variant["id"]))[0]["reason"] is None


async def test_receiving_a_nonpositive_quantity_is_refused(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)

    resp = await client.post(receive_url(variant), json={"quantity": 0})

    assert resp.status_code == 422, resp.text
    assert await movements(variant["id"]) == []


async def test_receiving_against_an_unknown_variant_404s(client):
    await as_admin(client)
    product = await make_product(client)

    resp = await client.post(
        f"{PRODUCTS}/{product['id']}/variants/{uuid.uuid4()}/receive", json={"quantity": 1}
    )

    assert resp.status_code == 404, resp.text


# --- the catalog routes never touch stock (R16) ----------------------------------------------


async def test_the_variant_patch_cannot_change_stock_and_history_stays_complete(client):
    """A new variant starts at zero; only receive/adjust move it, each with a movement row, so
    the movements always sum to what is on hand. The catalog PATCH (`catalog.manage`) and the
    create both refuse a stock count outright."""
    await as_admin(client)
    product = await make_product(client)
    created = await client.post(
        f"{PRODUCTS}/{product['id']}/variants",
        json={"name": "500ml", "sku": "SHMP-500", "quantity_on_hand": 40},
    )
    assert created.status_code == 422, created.text
    variant = (
        await client.post(
            f"{PRODUCTS}/{product['id']}/variants", json={"name": "500ml", "sku": "SHMP-500"}
        )
    ).json()["variants"][0]
    assert variant["quantity_on_hand"] == 0

    assert (await client.post(receive_url(variant), json={"quantity": 7})).status_code == 200
    patched = await client.patch(
        f"{PRODUCTS}/{product['id']}/variants/{variant['id']}", json={"quantity_on_hand": 99}
    )
    assert patched.status_code == 422, patched.text
    adjusted = await client.post(adjust_url(variant), json={"quantity_delta": -2, "reason": "x"})
    assert adjusted.status_code == 200, adjusted.text

    assert await quantity_on_hand(variant["id"]) == 5
    assert sum(m["quantity_delta"] for m in await movements(variant["id"])) == 5


# --- adjusting --------------------------------------------------------------------------------


async def test_adjusting_stock_up_and_down(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)

    up = await client.post(adjust_url(variant), json={"quantity_delta": 3, "reason": "recount"})
    assert up.status_code == 200, up.text
    assert up.json()["variants"][0]["quantity_on_hand"] == 13

    down = await client.post(
        adjust_url(variant), json={"quantity_delta": -5, "reason": "damaged stock"}
    )
    assert down.status_code == 200, down.text
    assert down.json()["variants"][0]["quantity_on_hand"] == 8

    rows = await movements(variant["id"])
    assert [r["kind"] for r in rows] == ["adjustment", "adjustment"]
    assert [r["quantity_delta"] for r in rows] == [3, -5]
    assert [r["reason"] for r in rows] == ["recount", "damaged stock"]


async def test_adjusting_stock_requires_a_reason(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)

    resp = await client.post(adjust_url(variant), json={"quantity_delta": 3})

    assert resp.status_code == 422, resp.text
    assert await movements(variant["id"]) == []


async def test_adjusting_stock_refuses_a_blank_reason(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)

    resp = await client.post(adjust_url(variant), json={"quantity_delta": 3, "reason": "   "})

    assert resp.status_code == 422, resp.text


async def test_adjusting_by_zero_is_refused(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)

    resp = await client.post(adjust_url(variant), json={"quantity_delta": 0, "reason": "no-op"})

    assert resp.status_code == 422, resp.text


# --- stock never goes negative, DB-enforced --------------------------------------------------


async def test_adjusting_below_zero_is_refused_and_writes_nothing(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=3)

    resp = await client.post(adjust_url(variant), json={"quantity_delta": -4, "reason": "count"})

    assert resp.status_code == 409, resp.text
    assert await quantity_on_hand(variant["id"]) == 3
    assert await movements(variant["id"]) == []


async def test_two_concurrent_adjustments_do_not_take_stock_negative(client, monkeypatch):
    """Two ASGI clients race to deduct the same last five units. Nothing in the app locks —
    the atomic `UPDATE ... WHERE quantity_on_hand + :delta >= 0` inside `record_movement` is
    the lock (CLAUDE.md "Concurrency": stock). The sleep tacked onto the *real* call keeps the
    first transaction open long enough that the second's own `UPDATE` genuinely blocks on
    Postgres's row lock rather than racing in application code — the same interleaving
    `test_appointments.py`'s concurrent-booking test forces with `offered_slot`."""
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=5)
    cookie = client.cookies["linsuite_session"]

    from inventory import stock_routes
    from main import app as main_app

    real_record_movement = stock_routes.record_movement

    async def slow_record_movement(*args, **kwargs):
        result = await real_record_movement(*args, **kwargs)
        await asyncio.sleep(0.25)
        return result

    monkeypatch.setattr(stock_routes, "record_movement", slow_record_movement)

    async def attempt():
        async with AsyncClient(transport=ASGITransport(app=main_app), base_url="http://test") as c:
            c.cookies.set("linsuite_session", cookie)
            return await c.post(
                adjust_url(variant), json={"quantity_delta": -5, "reason": "count correction"}
            )

    results = await asyncio.gather(attempt(), attempt())

    assert sorted(r.status_code for r in results) == [200, 409], [r.text for r in results]
    assert await quantity_on_hand(variant["id"]) == 0
    assert len(await movements(variant["id"])) == 1


# --- two distinct, independently grantable capabilities ---------------------------------------


async def test_receiving_needs_inventory_receive(client):
    await as_admin(client)
    variant = await new_variant(client)
    email, password = await deputy_with(client, [])
    client.cookies.clear()
    await as_admin(client, email, password)

    resp = await client.post(receive_url(variant), json={"quantity": 1})

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "capability_required"


async def test_adjusting_needs_inventory_adjust(client):
    await as_admin(client)
    variant = await new_variant(client)
    email, password = await deputy_with(client, [])
    client.cookies.clear()
    await as_admin(client, email, password)

    resp = await client.post(adjust_url(variant), json={"quantity_delta": 1, "reason": "x"})

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "capability_required"


async def test_inventory_receive_does_not_imply_inventory_adjust(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)
    email, password = await deputy_with(client, ["inventory.receive"])
    client.cookies.clear()
    await as_admin(client, email, password)

    receive = await client.post(receive_url(variant), json={"quantity": 1})
    adjust = await client.post(adjust_url(variant), json={"quantity_delta": 1, "reason": "x"})

    assert receive.status_code == 200, receive.text
    assert adjust.status_code == 403, adjust.text


async def test_inventory_adjust_does_not_imply_inventory_receive(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10)
    email, password = await deputy_with(client, ["inventory.adjust"])
    client.cookies.clear()
    await as_admin(client, email, password)

    adjust = await client.post(adjust_url(variant), json={"quantity_delta": 1, "reason": "x"})
    receive = await client.post(receive_url(variant), json={"quantity": 1})

    assert adjust.status_code == 200, adjust.text
    assert receive.status_code == 403, receive.text


# --- the ledger itself is append-only, by grant and trigger -----------------------------------


@pytest.mark.parametrize("statement", [f"UPDATE {TABLE} SET reason = 'x'", f"DELETE FROM {TABLE}"])
async def test_the_app_role_may_neither_update_nor_delete_a_movement(database, statement):
    async with session_scope() as db:
        with pytest.raises(DBAPIError) as refused:
            await db.execute(text(statement))
        await db.rollback()
    # 42501 insufficient_privilege: the grant refused it before the trigger had to.
    assert getattr(refused.value.orig, "sqlstate", None) == "42501", refused.value


async def test_the_append_only_trigger_is_attached_and_enabled(database):
    async with session_scope() as db:
        enabled = await db.scalar(
            text(
                "SELECT tgenabled::text FROM pg_trigger "
                f"WHERE tgrelid = '{TABLE}'::regclass AND tgname = '{TRIGGER}'"
            )
        )
    assert enabled == "O"
