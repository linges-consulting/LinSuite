"""S1/S2/S3: secure form links — issue, deliver, and the public form page (#46).

The first unauthenticated surface. What these pin down:

- the token exists only in the URL handed back once (and the email): the database keeps its
  SHA-256, and no column or audit row ever holds the token itself;
- a link renders the version it was issued against, whatever was published since;
- every way a link can be dead — unknown, expired, consumed, revoked, client erased, form
  retired — answers the same 404 body, so the page is not an oracle;
- the public answer carries the form and the business, the client's first name and nothing
  else of the profile, sets no cookie, writes no access-log row, and is not cached;
- issuing is `forms.issue` (Staff Mode), refused for an erased client, and erasure revokes
  what is open.
"""

import hashlib
import os
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from core.db import get_purge_engine, session_scope
from forms import links
from tests.test_access_log import add_role
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    CUSTOMERS,
    EMAIL,
    OTHER_PASSWORD,
    PASSWORD,
    add_colleague,
    as_admin,
    as_staff,
    claimed_instance,
    make_customer,
)
from tests.test_forms import _wipe_forms, make, publish, save, schema

PUBLIC = "/api/public/forms"
TEMPLATES = "/api/forms/templates"
BASE = "http://test.linsuite.example"
INVALID = {"detail": "This link is no longer valid.", "code": "link_invalid"}


@pytest.fixture(autouse=True)
async def forms_wiped(claimed_instance):  # noqa: F811 — the imported fixture, by name
    from core.redis import get_redis

    async for key in get_redis().scan_iter("public:forms:*"):
        await get_redis().delete(key)
    yield
    await _wipe_forms()


def links_path(customer_id: str) -> str:
    return f"{CUSTOMERS}/{customer_id}/form-links"


async def published_template(client, **draft) -> dict:
    """A published v1, issued from while still in Admin Mode (forms.manage)."""
    template = await make(client)
    await save(client, template, **draft)
    resp = await publish(client, template["id"])
    assert resp.status_code == 201, resp.text
    return template


async def issue(client, customer_id: str, template_id: str):
    return await client.post(links_path(customer_id), json={"template_id": template_id})


async def revoke(client, customer_id: str, link_id: str):
    return await client.post(f"{links_path(customer_id)}/{link_id}/revoke", json={})


def token_of(url: str) -> str:
    assert url.startswith(f"{BASE}/f/"), url
    return url.removeprefix(f"{BASE}/f/")


async def as_owner(sql: str, **params) -> None:
    owner = create_async_engine(os.environ["DATABASE_URL_MIGRATE"])
    try:
        async with owner.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await owner.dispose()


# --- S2: the token and its expiry --------------------------------------------------------------


def test_a_token_is_43_url_safe_characters_and_only_its_sha256_is_kept():
    token, digest = links.new_token()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43,}", token)
    assert digest == hashlib.sha256(token.encode()).digest() == links.digest(token)
    assert len(digest) == 32
    assert links.new_token()[0] != token


def test_a_link_expires_48_hours_after_issue():
    now = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)  # DST in Toronto starts the next night
    assert links.expires_at(now) == now + timedelta(hours=48)


# --- S1: issuing ------------------------------------------------------------------------------


async def test_issuing_returns_the_url_once_and_the_database_keeps_only_its_hash(client):
    await as_admin(client)
    template = await published_template(client)
    customer_id = await make_customer(client)

    resp = await issue(client, customer_id, template["id"])

    assert resp.status_code == 201, resp.text
    body = resp.json()
    token = token_of(body["url"])
    assert re.fullmatch(r"[A-Za-z0-9_-]{43,}", token)
    async with session_scope() as db:
        row = (
            await db.execute(
                text("SELECT *, row_to_json(l)::text AS whole FROM form_links l WHERE id = :id"),
                {"id": body["id"]},
            )
        ).one()
    assert bytes(row.token_sha256) == hashlib.sha256(token.encode()).digest()
    assert token not in row.whole
    assert row.expires_at - row.issued_at == timedelta(hours=48)
    assert datetime.fromisoformat(body["expires_at"]) == row.expires_at
    async with get_purge_engine().connect() as purge:
        events = (
            await purge.execute(
                text(
                    "SELECT event_type, target_id, metadata, row_to_json(a)::text AS whole "
                    "FROM audit_events a WHERE event_type LIKE 'form.link%'"
                )
            )
        ).all()
    assert [(e.event_type, e.target_id) for e in events] == [("form.link_issued", body["id"])]
    assert events[0].metadata == {
        "customer_id": customer_id,
        "template_id": template["id"],
        "version": 1,
    }
    assert all(token not in e.whole for e in events)
    listed = (await client.get(links_path(customer_id))).json()["links"]
    assert [link["id"] for link in listed] == [body["id"]]
    assert token not in str(listed) and "url" not in listed[0]


async def test_a_link_renders_the_version_it_was_issued_against(client):
    await as_admin(client)
    template = await published_template(client)
    customer_id = await make_customer(client)
    first = token_of((await issue(client, customer_id, template["id"])).json()["url"])
    v1 = (await client.get(f"{PUBLIC}/{first}")).json()

    await save(client, template, schema=schema("Could you be pregnant?"))
    assert (await publish(client, template["id"])).status_code == 201
    second = token_of((await issue(client, customer_id, template["id"])).json()["url"])

    again = await client.get(f"{PUBLIC}/{first}")
    assert again.json()["version_id"] == v1["version_id"]
    assert again.json()["schema"]["fields"][0]["label"] == "Are you pregnant?"
    newer = (await client.get(f"{PUBLIC}/{second}")).json()
    assert newer["version_id"] != v1["version_id"]
    assert newer["schema"]["fields"][0]["label"] == "Could you be pregnant?"


async def test_every_dead_link_answers_the_same_404(client):
    await as_admin(client)
    template = await published_template(client)
    customer_id = await make_customer(client)

    async def fresh() -> tuple[str, str]:
        body = (await issue(client, customer_id, template["id"])).json()
        return body["id"], token_of(body["url"])

    expired_id, expired = await fresh()
    await as_owner(
        "UPDATE form_links SET issued_at = now() - interval '3 days', "
        "expires_at = now() - interval '1 day' WHERE id = :id",
        id=expired_id,
    )
    consumed_id, consumed = await fresh()
    await as_owner("UPDATE form_links SET consumed_at = now() WHERE id = :id", id=consumed_id)
    revoked_id, revoked = await fresh()
    resp = await revoke(client, customer_id, revoked_id)
    assert resp.status_code == 204, resp.text
    _, erased = await fresh()
    resp = await client.post(f"{CUSTOMERS}/{customer_id}/erasure", json={})
    assert resp.status_code == 201, resp.text

    other = await make_customer(client)
    _, suppressed = await fresh_for(client, other, template["id"])
    # Suppressed without the erasure path's revocation: the page checks the client itself.
    await as_owner("UPDATE customers SET suppressed_at = now() WHERE id = :id", id=other)
    third = await make_customer(client)
    _, retired = await fresh_for(client, third, template["id"])
    retire = await client.post(f"/api/admin/forms/{template['id']}/retire", json={})
    assert retire.status_code == 200

    client.cookies.clear()
    unknown, _ = links.new_token()
    for token in (unknown, expired, consumed, revoked, erased, suppressed, retired, "x", "a" * 500):
        resp = await client.get(f"{PUBLIC}/{token}")
        assert resp.status_code == 404, token
        assert resp.json() == INVALID, token
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["referrer-policy"] == "no-referrer"


async def fresh_for(client, customer_id: str, template_id: str) -> tuple[str, str]:
    body = (await issue(client, customer_id, template_id)).json()
    return body["id"], token_of(body["url"])


async def test_issuing_is_refused_for_an_erased_client_and_erasure_revokes_open_links(client):
    await as_admin(client)
    template = await published_template(client)
    customer_id = await make_customer(client)
    open_id, _ = await fresh_for(client, customer_id, template["id"])

    assert (await client.post(f"{CUSTOMERS}/{customer_id}/erasure", json={})).status_code == 201

    async with session_scope() as db:
        revoked_at = await db.scalar(
            text("SELECT revoked_at FROM form_links WHERE id = :id"), {"id": open_id}
        )
    assert revoked_at is not None
    resp = await issue(client, customer_id, template["id"])
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "customer_suppressed"


async def test_a_retired_or_unpublished_form_cannot_be_sent(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    draft_only = await make(client, name="Draft only")
    resp = await issue(client, customer_id, draft_only["id"])
    assert resp.status_code == 409 and resp.json()["code"] == "template_unpublished"

    retired = await published_template(client)
    await client.post(f"/api/admin/forms/{retired['id']}/retire", json={})
    resp = await issue(client, customer_id, retired["id"])
    assert resp.status_code == 409 and resp.json()["code"] == "template_retired"


async def test_sending_forms_needs_forms_issue_and_works_in_staff_mode(client):
    await as_admin(client)
    template = await published_template(client)
    await make(client, name="Never published")
    customer_id = await make_customer(client)
    await add_role(client, "Viewer", ["customers.view"], "viewer@cedar.example")
    await add_colleague(client, "desk@cedar.example", OTHER_PASSWORD)  # the seeded Staff role

    client.cookies.clear()
    await as_staff(client, "viewer@cedar.example", OTHER_PASSWORD)
    for resp in (
        await issue(client, customer_id, template["id"]),
        await client.get(links_path(customer_id)),
        await client.get(TEMPLATES),
    ):
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "capability_required"

    client.cookies.clear()
    await as_staff(client, "desk@cedar.example", OTHER_PASSWORD)
    offered = (await client.get(TEMPLATES)).json()["templates"]
    assert [(t["template_id"], t["name"], t["version"]) for t in offered] == [
        (template["id"], "Prenatal intake", 1)
    ]
    resp = await issue(client, customer_id, template["id"])
    assert resp.status_code == 201, resp.text


# --- S1: the public page ----------------------------------------------------------------------


async def test_the_public_form_carries_the_form_the_business_and_a_first_name_only(client):
    await as_admin(client)
    template = await published_template(client)
    customer_id = await make_customer(client)
    token = token_of((await issue(client, customer_id, template["id"])).json()["url"])
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_access_log"))

    # Signed in or not, the same answer, and the staff session is neither read nor renewed.
    for signed_in in (True, False):
        if not signed_in:
            client.cookies.clear()
        resp = await client.get(f"{PUBLIC}/{token}")
        assert resp.status_code == 200, resp.text
        assert "set-cookie" not in resp.headers
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["referrer-policy"] == "no-referrer"
        body = resp.json()
        assert set(body) == {
            "version_id",
            "template_name",
            "schema",
            "business",
            "client_first_name",
            "expires_at",
        }
        assert body["template_name"] == "Prenatal intake"
        assert body["client_first_name"] == "Priya"
        assert body["business"] == {"name": "Cedar Lane Clinic", "logo_url": None}
        assert "Nair" not in resp.text and "4165550199" not in resp.text

    async with session_scope() as db:
        assert await db.scalar(text("SELECT count(*) FROM audit_access_log")) == 0
    async with get_purge_engine().connect() as purge:
        opened = (
            await purge.execute(
                text(
                    "SELECT target_id, actor_user_id FROM audit_events "
                    "WHERE event_type = 'form.link_opened'"
                )
            )
        ).all()
    assert len(opened) == 2 and all(o.actor_user_id is None for o in opened)


async def test_public_lookups_are_rate_limited_per_address(client):
    unknown, _ = links.new_token()
    for _ in range(links.PUBLIC_LOOKUPS_PER_MINUTE):
        assert (await client.get(f"{PUBLIC}/{unknown}")).status_code == 404
    resp = await client.get(f"{PUBLIC}/{unknown}")
    assert resp.status_code == 429
    assert int(resp.headers["retry-after"]) > 0


# --- S3: delivery -----------------------------------------------------------------------------


async def test_the_link_is_emailed_when_the_client_has_an_address(client, sent_emails):
    await as_admin(client)
    template = await published_template(client)
    with_email = (
        await client.post(
            CUSTOMERS, json={"first_name": "Ana", "last_name": "Lee", "email": "ana@x.example"}
        )
    ).json()["id"]
    without = await make_customer(client)

    emailed = (await issue(client, with_email, template["id"])).json()
    assert emailed["emailed_to"] == "ana@x.example"
    assert [m.to for m in sent_emails] == ["ana@x.example"]
    assert emailed["url"] in sent_emails[0].text

    sent_emails.clear()
    silent = (await issue(client, without, template["id"])).json()
    assert silent["emailed_to"] is None
    assert sent_emails == []


# --- S1: open links and revocation ------------------------------------------------------------


async def test_revoking_closes_a_link_and_drops_it_from_the_open_list(client):
    await as_admin(client)
    template = await published_template(client)
    customer_id = await make_customer(client)
    link_id, token = await fresh_for(client, customer_id, template["id"])
    listed = (await client.get(links_path(customer_id))).json()["links"]
    assert listed[0]["template_name"] == "Prenatal intake" and listed[0]["version"] == 1

    assert (await revoke(client, customer_id, link_id)).status_code == 204
    assert (await client.get(links_path(customer_id))).json()["links"] == []
    assert (await client.get(f"{PUBLIC}/{token}")).status_code == 404
    assert (await revoke(client, customer_id, link_id)).status_code == 404
    other = await make_customer(client)
    other_link, _ = await fresh_for(client, other, template["id"])
    # Another client's link through this client's path is nobody's link.
    assert (await revoke(client, customer_id, other_link)).status_code == 404
