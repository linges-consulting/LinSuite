"""S1: the retail catalog — products and the variants that actually carry a SKU, a barcode,
a price and a stock count (M4 #56).

**Two tables, and the split matters.** "Shampoo" is a product; "Shampoo, 500ml" is a
variant, and only the variant is bought, has a SKU, or runs out. Whole-unit stock and its own
low-stock threshold live on the variant, independent of any sibling.

**No stock movements yet** — #61 adds the atomic decrement. This ticket is the catalog shape
and starting counts only.

**No hard delete**, same as `services`/`resources`: `active` false takes a row off
`GET /api/catalog/products` while any later invoice reference keeps pointing at something
real.
"""

import pytest
from sqlalchemy import text

from core.db import get_purge_engine, session_scope

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": EMAIL,
    "admin_password": PASSWORD,
}

PRODUCTS = "/api/admin/products"
CATALOG = "/api/catalog/products"


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
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


async def as_admin(client, email=EMAIL, password=PASSWORD):
    login = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


def product_draft(**overrides) -> dict:
    body = {"name": "Shampoo", "description": "Sulphate-free.", "sort_order": 0}
    body.update(overrides)
    return body


def variant_draft(**overrides) -> dict:
    body = {
        "name": "500ml",
        "sku": "SHMP-500",
        "barcode": "0123456789012",
        "price_cents": 2499,
        "quantity_on_hand": 40,
        "low_stock_threshold": 5,
    }
    body.update(overrides)
    return body


async def make_product(client, **overrides) -> dict:
    resp = await client.post(PRODUCTS, json=product_draft(**overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def make_variant(client, product_id: str, **overrides) -> dict:
    resp = await client.post(f"{PRODUCTS}/{product_id}/variants", json=variant_draft(**overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def events() -> list[str]:
    async with session_scope() as db:
        rows = (
            await db.execute(text("SELECT event_type FROM audit_events ORDER BY occurred_at, id"))
        ).all()
    return [row[0] for row in rows]


# --- creating a product -----------------------------------------------------------------------


async def test_creating_a_product(client):
    await as_admin(client)

    created = await make_product(client)

    assert created["name"] == "Shampoo"
    assert created["description"] == "Sulphate-free."
    assert created["active"] is True
    assert created["variants"] == []
    assert "product.created" in await events()


async def test_product_names_are_unique_case_insensitively(client):
    await as_admin(client)
    await make_product(client, name="Shampoo")

    dupe = await client.post(PRODUCTS, json=product_draft(name="shampoo"))

    assert dupe.status_code == 409, dupe.text


# --- creating a variant ------------------------------------------------------------------------


async def test_creating_a_variant(client):
    await as_admin(client)
    product = await make_product(client)

    variant = await make_variant(client, product["id"])

    assert variant["variants"][0]["name"] == "500ml"
    assert variant["variants"][0]["sku"] == "SHMP-500"
    assert variant["variants"][0]["barcode"] == "0123456789012"
    assert variant["variants"][0]["price_cents"] == 2499
    assert variant["variants"][0]["quantity_on_hand"] == 40
    assert variant["variants"][0]["low_stock_threshold"] == 5
    assert variant["variants"][0]["active"] is True
    assert "product_variant.created" in await events()


async def test_a_product_may_have_more_than_one_variant_each_with_its_own_threshold(client):
    await as_admin(client)
    product = await make_product(client)
    await make_variant(client, product["id"], name="500ml", sku="SHMP-500", low_stock_threshold=5)

    resp = await client.post(
        f"{PRODUCTS}/{product['id']}/variants",
        json=variant_draft(
            name="1L", sku="SHMP-1000", barcode="0123456789029", low_stock_threshold=2
        ),
    )

    assert resp.status_code == 201, resp.text
    thresholds = {v["name"]: v["low_stock_threshold"] for v in resp.json()["variants"]}
    assert thresholds == {"500ml": 5, "1L": 2}


@pytest.mark.parametrize(
    "field,value",
    [
        ("price_cents", -1),
        ("quantity_on_hand", -1),
        ("low_stock_threshold", -1),
        ("quantity_on_hand", 1.5),
    ],
)
async def test_stock_and_money_are_refused_when_nonsense(client, field, value):
    await as_admin(client)
    product = await make_product(client)

    resp = await client.post(
        f"{PRODUCTS}/{product['id']}/variants", json=variant_draft(**{field: value})
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"][-1] == field


async def test_variant_names_are_unique_within_a_product_but_not_across_products(client):
    await as_admin(client)
    a = await make_product(client, name="Shampoo")
    b = await make_product(client, name="Conditioner")
    await make_variant(client, a["id"], name="Large", sku="SHMP-L", barcode="1111111111111")

    dupe_in_a = await client.post(
        f"{PRODUCTS}/{a['id']}/variants",
        json=variant_draft(name="large", sku="SHMP-L2", barcode="2222222222222"),
    )
    ok_in_b = await client.post(
        f"{PRODUCTS}/{b['id']}/variants",
        json=variant_draft(name="Large", sku="COND-L", barcode="3333333333333"),
    )

    assert dupe_in_a.status_code == 409, dupe_in_a.text
    assert ok_in_b.status_code == 201, ok_in_b.text


async def test_skus_are_unique_across_the_whole_catalog(client):
    await as_admin(client)
    a = await make_product(client, name="Shampoo")
    b = await make_product(client, name="Conditioner")
    await make_variant(client, a["id"], sku="SAME-SKU", barcode="1111111111111")

    dupe = await client.post(
        f"{PRODUCTS}/{b['id']}/variants",
        json=variant_draft(name="1L", sku="same-sku", barcode="2222222222222"),
    )

    assert dupe.status_code == 409, dupe.text


async def test_barcodes_are_unique_across_the_whole_catalog(client):
    await as_admin(client)
    a = await make_product(client, name="Shampoo")
    b = await make_product(client, name="Conditioner")
    await make_variant(client, a["id"], sku="SHMP-500", barcode="1111111111111")

    dupe = await client.post(
        f"{PRODUCTS}/{b['id']}/variants",
        json=variant_draft(name="1L", sku="COND-1000", barcode="1111111111111"),
    )

    assert dupe.status_code == 409, dupe.text


async def test_more_than_one_variant_may_carry_no_barcode(client):
    await as_admin(client)
    product = await make_product(client)
    await make_variant(client, product["id"], name="500ml", sku="SHMP-500", barcode=None)

    resp = await client.post(
        f"{PRODUCTS}/{product['id']}/variants",
        json=variant_draft(name="1L", sku="SHMP-1000", barcode=None),
    )

    assert resp.status_code == 201, resp.text


# --- editing -------------------------------------------------------------------------------


async def test_editing_a_variant_records_only_the_fields_that_changed(client):
    await as_admin(client)
    product = await make_variant(client, (await make_product(client))["id"])
    variant = product["variants"][0]

    resp = await client.patch(
        f"{PRODUCTS}/{variant['product_id']}/variants/{variant['id']}",
        json={"quantity_on_hand": 12},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["variants"][0]["quantity_on_hand"] == 12
    updated = [row for row in await events() if row == "product_variant.updated"]
    assert len(updated) == 1


@pytest.mark.parametrize(
    "field", ["name", "sku", "price_cents", "quantity_on_hand", "low_stock_threshold"]
)
async def test_emptying_a_required_variant_field_is_refused_rather_than_a_five_hundred(
    client, field
):
    await as_admin(client)
    product = await make_variant(client, (await make_product(client))["id"])
    variant = product["variants"][0]

    resp = await client.patch(
        f"{PRODUCTS}/{variant['product_id']}/variants/{variant['id']}", json={field: None}
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", field]


async def test_the_barcode_may_be_cleared(client):
    await as_admin(client)
    product = await make_variant(client, (await make_product(client))["id"])
    variant = product["variants"][0]

    resp = await client.patch(
        f"{PRODUCTS}/{variant['product_id']}/variants/{variant['id']}", json={"barcode": None}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["variants"][0]["barcode"] is None


# --- deactivating: no hard delete ---------------------------------------------------------------


async def test_deactivating_a_variant_keeps_the_row_and_drops_it_from_the_catalog(client):
    await as_admin(client)
    product = await make_variant(client, (await make_product(client))["id"])
    variant = product["variants"][0]

    resp = await client.post(
        f"{PRODUCTS}/{variant['product_id']}/variants/{variant['id']}/deactivate", json={}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["variants"][0]["active"] is False

    # The admin listing still shows it — this is the editing surface.
    listed = await client.get(PRODUCTS)
    assert listed.json()["products"][0]["variants"][0]["active"] is False

    # The checkout catalog does not — nothing there can be sold.
    catalog = await client.get(CATALOG)
    assert catalog.json()["products"][0]["variants"] == []


async def test_deactivating_a_product_leaves_its_variants_as_they_were(client):
    await as_admin(client)
    product = await make_variant(client, (await make_product(client))["id"])

    resp = await client.post(f"{PRODUCTS}/{product['id']}/deactivate", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["active"] is False
    assert resp.json()["variants"][0]["active"] is True

    catalog = await client.get(CATALOG)
    assert catalog.json()["products"] == []  # the product itself is inactive


async def test_reactivating_a_product(client):
    await as_admin(client)
    product = await make_product(client)
    await client.post(f"{PRODUCTS}/{product['id']}/deactivate", json={})

    resp = await client.post(f"{PRODUCTS}/{product['id']}/reactivate", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["active"] is True


# --- the catalog read side ---------------------------------------------------------------------


async def test_somebody_who_only_checks_out_may_read_the_catalog(client):
    from core.security import hash_password
    from tests.conftest import add_account

    await as_admin(client)
    await make_variant(client, (await make_product(client))["id"])
    front_password = "correct horse battery 2"
    await add_account("front@cedar.example", await hash_password(front_password))
    client.cookies.clear()
    login = await client.post(
        "/api/auth/login", json={"email": "front@cedar.example", "password": front_password}
    )
    assert login.status_code == 200  # Staff Mode, no `catalog.manage`

    resp = await client.get(CATALOG)

    assert resp.status_code == 200, resp.text
    assert [p["name"] for p in resp.json()["products"]] == ["Shampoo"]


async def test_the_catalog_read_endpoint_refuses_a_stranger(client):
    resp = await client.get(CATALOG)

    assert resp.status_code == 401, resp.text


# --- who may do any of this ---------------------------------------------------------------------


async def test_every_product_endpoint_needs_catalog_manage(client):
    from core.security import hash_password
    from tests.conftest import add_account

    await as_admin(client)
    product = await make_variant(client, (await make_product(client))["id"])
    variant = product["variants"][0]
    role = await client.post(
        "/api/admin/roles",
        json={"name": "Deputy", "description": "Nearly.", "capabilities": ["admin"]},
    )
    assert role.status_code == 201, role.text
    deputy_password = "correct horse battery 2"
    await add_account(
        "deputy@cedar.example", await hash_password(deputy_password), role=role.json()["id"]
    )
    client.cookies.clear()
    await as_admin(client, "deputy@cedar.example", deputy_password)

    refusals = [
        await client.get(PRODUCTS),
        await client.post(PRODUCTS, json=product_draft(name="nope")),
        await client.patch(f"{PRODUCTS}/{product['id']}", json={"sort_order": 3}),
        await client.post(f"{PRODUCTS}/{product['id']}/deactivate", json={}),
        await client.post(f"{PRODUCTS}/{product['id']}/reactivate", json={}),
        await client.post(f"{PRODUCTS}/{product['id']}/variants", json=variant_draft(sku="X")),
        await client.patch(
            f"{PRODUCTS}/{product['id']}/variants/{variant['id']}", json={"sort_order": 1}
        ),
        await client.post(
            f"{PRODUCTS}/{product['id']}/variants/{variant['id']}/deactivate", json={}
        ),
        await client.post(
            f"{PRODUCTS}/{product['id']}/variants/{variant['id']}/reactivate", json={}
        ),
    ]

    assert [r.status_code for r in refusals] == [403] * len(refusals)
    assert {r.json()["code"] for r in refusals} == {"capability_required"}
