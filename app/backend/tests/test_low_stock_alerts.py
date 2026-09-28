"""S1: low-stock alerts (#62) — the armed/already-alerted crossing on `product_variants`, the
always-shown in-app `is_low_stock` field, and the opt-in email alert queued through Celery
after commit.

Reuses `test_stock_movements.py`'s `claimed_instance` fixture and `new_variant`/`adjust_url`/
`receive_url` helpers (cross-file test imports are an established pattern here — see that
file's own docstring and the M4 #59 ledger entry) rather than duplicating the setup.
"""

import asyncio

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from core.db import session_scope
from tests.test_form_links import make_email_ready
from tests.test_inventory import as_admin
from tests.test_stock_movements import (  # noqa: F401 — the autouse fixture comes along
    adjust_url,
    claimed_instance,
    new_variant,
    receive_url,
)

PRODUCTS = "/api/admin/products"


async def opt_in_with_contact_email(email: str = "owner@cedar.example") -> None:
    async with session_scope() as db:
        await db.execute(
            text("UPDATE businesses SET low_stock_alert_email_enabled = true, email = :email"),
            {"email": email},
        )
        await db.commit()


async def is_alerted(variant_id: str) -> bool:
    async with session_scope() as db:
        return await db.scalar(
            text("SELECT low_stock_alerted FROM product_variants WHERE id = :id"),
            {"id": variant_id},
        )


# --- in-app warning: always shown, independent of the email opt-in -------------------------


async def test_a_variant_above_its_threshold_is_not_flagged(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10, low_stock_threshold=5)

    assert variant["is_low_stock"] is False


async def test_crossing_below_the_threshold_flags_is_low_stock_even_with_email_opted_out(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10, low_stock_threshold=5)

    resp = await client.post(adjust_url(variant), json={"quantity_delta": -6, "reason": "sold"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["variants"][0]["is_low_stock"] is True
    assert await is_alerted(variant["id"]) is True


async def test_restocking_back_to_the_threshold_clears_the_flag(client):
    await as_admin(client)
    variant = await new_variant(client, quantity_on_hand=10, low_stock_threshold=5)
    await client.post(adjust_url(variant), json={"quantity_delta": -6, "reason": "sold"})

    resp = await client.post(receive_url(variant), json={"quantity": 1})  # 4 -> 5, at threshold

    assert resp.json()["variants"][0]["is_low_stock"] is False
    assert await is_alerted(variant["id"]) is False


# --- exactly one email per crossing ----------------------------------------------------------


async def test_no_email_is_queued_while_the_opt_in_is_off(client, sent_emails):
    await as_admin(client)
    await make_email_ready()  # sender configured and verified, opt-in still off by default
    variant = await new_variant(client, quantity_on_hand=10, low_stock_threshold=5)

    resp = await client.post(adjust_url(variant), json={"quantity_delta": -6, "reason": "sold"})

    assert resp.status_code == 200, resp.text
    assert sent_emails == []


async def test_crossing_below_threshold_queues_exactly_one_email_once_opted_in(client, sent_emails):
    await as_admin(client)
    await make_email_ready()
    await opt_in_with_contact_email()
    variant = await new_variant(
        client, name="500ml", sku="SHMP-LOW", quantity_on_hand=10, low_stock_threshold=5
    )

    resp = await client.post(adjust_url(variant), json={"quantity_delta": -6, "reason": "sold"})

    assert resp.status_code == 200, resp.text
    assert len(sent_emails) == 1
    assert sent_emails[0].to == "owner@cedar.example"
    assert "500ml" in sent_emails[0].text
    assert "SHMP-LOW" in sent_emails[0].text
    assert "4" in sent_emails[0].text  # quantity_on_hand after the sale


async def test_a_second_sale_that_stays_below_threshold_sends_no_further_email(client, sent_emails):
    await as_admin(client)
    await make_email_ready()
    await opt_in_with_contact_email()
    variant = await new_variant(client, quantity_on_hand=10, low_stock_threshold=5)
    # 10 -> 4, crosses.
    await client.post(adjust_url(variant), json={"quantity_delta": -6, "reason": "sold"})
    assert len(sent_emails) == 1

    resp = await client.post(adjust_url(variant), json={"quantity_delta": -2, "reason": "sold"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["variants"][0]["quantity_on_hand"] == 2
    assert len(sent_emails) == 1  # still just the one


async def test_restocking_then_crossing_again_alerts_a_second_time(client, sent_emails):
    await as_admin(client)
    await make_email_ready()
    await opt_in_with_contact_email()
    variant = await new_variant(client, quantity_on_hand=10, low_stock_threshold=5)
    # 10 -> 4, alerts.
    await client.post(adjust_url(variant), json={"quantity_delta": -6, "reason": "sold"})
    assert len(sent_emails) == 1

    await client.post(receive_url(variant), json={"quantity": 10})  # 4 -> 14, rearms
    # 14 -> 4, crosses again.
    resp = await client.post(adjust_url(variant), json={"quantity_delta": -10, "reason": "sold"})

    assert resp.status_code == 200, resp.text
    assert len(sent_emails) == 2


async def test_an_unconfigured_sender_degrades_silently_no_crash_no_send(client, sent_emails):
    await as_admin(client)
    await opt_in_with_contact_email()  # opted in, but no sender ever configured/verified
    variant = await new_variant(client, quantity_on_hand=10, low_stock_threshold=5)

    resp = await client.post(adjust_url(variant), json={"quantity_delta": -6, "reason": "sold"})

    assert resp.status_code == 200, resp.text
    assert sent_emails == []


async def test_with_no_contact_email_on_file_nothing_is_sent(client, sent_emails):
    await as_admin(client)
    await make_email_ready()
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET low_stock_alert_email_enabled = true"))
        await db.commit()
    variant = await new_variant(client, quantity_on_hand=10, low_stock_threshold=5)

    resp = await client.post(adjust_url(variant), json={"quantity_delta": -6, "reason": "sold"})

    assert resp.status_code == 200, resp.text
    assert sent_emails == []


# --- exactly one alert under genuine concurrency ----------------------------------------------


async def test_two_concurrent_sales_crossing_the_threshold_at_once_send_exactly_one_alert(
    client, sent_emails, monkeypatch
):
    """Same interleaving `test_stock_movements.py::
    test_two_concurrent_adjustments_do_not_take_stock_negative` forces: a delay tacked onto the
    *real* `record_movement` call keeps the first transaction open long enough that the
    second's own `UPDATE` genuinely blocks on Postgres's row lock, rather than racing in
    application code. Starting at 6 with a threshold of 5, two concurrent -1 adjustments only
    let one of them actually cross (6->5 is still at the threshold, not below it; 5->4 is the
    real crossing) — the DB-serialized read of `low_stock_alerted` inside `record_movement` is
    what keeps that to exactly one armed alert, never two."""
    await as_admin(client)
    await make_email_ready()
    await opt_in_with_contact_email()
    variant = await new_variant(client, quantity_on_hand=6, low_stock_threshold=5)
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
            return await c.post(adjust_url(variant), json={"quantity_delta": -1, "reason": "sale"})

    results = await asyncio.gather(attempt(), attempt())

    assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
    assert await is_alerted(variant["id"]) is True
    assert len(sent_emails) == 1
