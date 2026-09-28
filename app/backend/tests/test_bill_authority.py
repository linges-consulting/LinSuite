"""S1: bill review authority (#64) — the two override paths on top of #63's bill review
screen, over a real PostgreSQL and Redis: staff-request review (persisted, admin-decided,
stale-approval refused) and inline admin edit (own credentials, verified on the staff screen,
attributed to the admin, bounded by a short window). Both paths' own business-setting toggle
is proven server-enforced, not merely hidden — a direct call with it off gets a real 403.

Reuses `tests/test_bill_review.py`'s fixture and helpers: the only way to get a real draft
bill is to book, confirm and complete a real appointment, and that file already does it.
"""

import asyncio

import pyotp
from sqlalchemy import text

from auth import mfa
from billing.bill_authority import _inline_admin_key
from core.db import session_scope
from core.redis import get_redis
from core.security import hash_password
from tests.conftest import add_account
from tests.test_bill_review import (  # noqa: F401 — the autouse fixture comes along
    BILLS,
    EMAIL,
    PASSWORD,
    STAFF_EMAIL,
    add_front_desk_account,
    as_admin,
    as_staff,
    claimed_instance,
    complete_a_visit,
)


def override_requests_url(bill_id: str) -> str:
    return f"{BILLS}/{bill_id}/override-requests"


def decision_url(bill_id: str, request_id: str) -> str:
    return f"{BILLS}/{bill_id}/override-requests/{request_id}/decision"


def inline_url(bill_id: str, action: str = "") -> str:
    suffix = f"/{action}" if action else ""
    return f"{BILLS}/{bill_id}/inline-admin{suffix}"


async def set_toggle(name: str, value: bool) -> None:
    async with session_scope() as db:
        await db.execute(text(f"UPDATE businesses SET {name} = :v"), {"v": value})
        await db.commit()


async def make_role(client, name: str, capabilities: list[str]) -> str:
    resp = await client.post("/api/admin/roles", json={"name": name, "capabilities": capabilities})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def submit_request(client, bill_id: str, **overrides) -> dict:
    body = {
        "kind": "price_override",
        "requested_total_cents": 9000,
        "reason": "Client complained about the finish, offered a reduced rate.",
    }
    body.update(overrides)
    resp = await client.post(override_requests_url(bill_id), json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def totp_code(secret: str) -> str:
    return pyotp.TOTP(secret).now()


async def enrol_mfa(client, email: str = EMAIL) -> str:
    """Enrols TOTP for whichever account the client is currently signed in as, and forgets the
    step spent confirming it — the same call `tests/test_mfa.py::enrol` makes, for the same
    reason: a code just used to confirm enrolment is "already used" to any later check within
    the same 30-second step, and this test goes on to authenticate with a fresh code from the
    same secret moments later."""
    started = await client.post("/api/auth/mfa/enrol", json={})
    assert started.status_code == 200, started.text
    secret = started.json()["secret"]
    confirmed = await client.post("/api/auth/mfa/enrol/confirm", json={"code": totp_code(secret)})
    assert confirmed.status_code == 200, confirmed.text
    async with session_scope() as db:
        uid = await db.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": email})
    await mfa.forget_used_steps(uid)
    return secret


# --- (a) staff-request review ---------------------------------------------------------------


async def test_staff_can_submit_an_override_request(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    body = await submit_request(client, bill_id, requested_total_cents=9500, reason="Loyal client")

    assert body["status"] == "pending"
    assert body["requested_total_cents"] == 9500
    assert body["requested_by_email"] == STAFF_EMAIL
    assert body["decided_by_email"] is None
    assert body["decided_total_cents"] is None


async def test_the_request_is_persisted_with_bill_revision_and_timestamp(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)

    body = await submit_request(client, bill_id)

    async with session_scope() as db:
        row = (
            await db.execute(
                text(
                    "SELECT bill_revision_as_of, requested_at FROM bill_override_requests "
                    "WHERE id = :id"
                ),
                {"id": body["id"]},
            )
        ).one()
    assert row.bill_revision_as_of is not None
    assert row.requested_at is not None
    assert body["bill_revision_as_of"] is not None


async def test_admin_can_approve_as_is_and_staff_resumes_ordinary_billing(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client, price_cents=12000)
    request = await submit_request(client, bill_id, requested_total_cents=9000)

    decided = await client.post(decision_url(bill_id, request["id"]), json={"decision": "approved"})

    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"
    assert decided.json()["decided_total_cents"] == 9000
    assert decided.json()["decided_by_email"] == EMAIL

    read = await client.get(f"{BILLS}/{bill_id}")
    assert read.status_code == 200, read.text
    assert read.json()["override_total_cents"] == 9000

    # "staff can resume ordinary billing on the same draft" — the ordinary discount route
    # still works on this same bill, with no separate unlock step.
    resumed = await client.put(f"{BILLS}/{bill_id}/discounts", json={"discount_ids": []})
    assert resumed.status_code == 200, resumed.text


async def test_admin_can_revise_the_amount_on_approval(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    request = await submit_request(client, bill_id, requested_total_cents=9000)

    decided = await client.post(
        decision_url(bill_id, request["id"]),
        json={"decision": "approved", "decided_total_cents": 8500, "note": "Split the difference"},
    )

    assert decided.status_code == 200, decided.text
    assert decided.json()["decided_total_cents"] == 8500
    read = await client.get(f"{BILLS}/{bill_id}")
    assert read.json()["override_total_cents"] == 8500
    assert read.json()["override_reason"] == "Split the difference"


async def test_admin_can_reject_a_request(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    request = await submit_request(client, bill_id)

    decided = await client.post(decision_url(bill_id, request["id"]), json={"decision": "rejected"})

    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "rejected"
    assert decided.json()["decided_total_cents"] is None
    read = await client.get(f"{BILLS}/{bill_id}")
    assert read.json()["override_total_cents"] is None


async def test_a_stale_request_cannot_be_approved(client):
    """The ticket's central requirement: a stale approval must not authorize a *different*
    exceptional change than the one actually reviewed. Here the bill moves on (an ordinary
    discount applied) between the request and the decision."""
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    request = await submit_request(client, bill_id)
    discount = await client.post(
        "/api/admin/discounts",
        json={
            "name": "Autumn",
            "kind": "percentage",
            "percentage_bp": 500,
            "commission_basis": "reduces",
        },
    )
    assert discount.status_code == 201, discount.text
    applied = await client.put(
        f"{BILLS}/{bill_id}/discounts", json={"discount_ids": [discount.json()["id"]]}
    )
    assert applied.status_code == 200, applied.text

    decided = await client.post(decision_url(bill_id, request["id"]), json={"decision": "approved"})

    assert decided.status_code == 409, decided.text
    assert "changed" in decided.json()["detail"]
    # Not silently applied.
    read = await client.get(f"{BILLS}/{bill_id}")
    assert read.json()["override_total_cents"] is None


async def test_a_decided_request_cannot_be_decided_again(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    request = await submit_request(client, bill_id)
    first = await client.post(decision_url(bill_id, request["id"]), json={"decision": "approved"})
    assert first.status_code == 200, first.text

    second = await client.post(decision_url(bill_id, request["id"]), json={"decision": "rejected"})

    assert second.status_code == 409, second.text


async def test_submitting_is_refused_with_the_toggle_off(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    await set_toggle("enable_bill_override_requests", False)

    resp = await client.post(
        override_requests_url(bill_id),
        json={"kind": "discount", "requested_total_cents": 9000, "reason": "x"},
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "bill_override_requests_disabled"


async def test_deciding_an_existing_request_still_works_with_the_toggle_off(client):
    """Disabling a path prevents *new* use of it; it never erases existing request history or
    the admin/owner's own ability to act on what is already pending (#54's "Override
    configuration" decision, verbatim)."""
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    request = await submit_request(client, bill_id)
    await set_toggle("enable_bill_override_requests", False)

    decided = await client.post(decision_url(bill_id, request["id"]), json={"decision": "approved"})

    assert decided.status_code == 200, decided.text


async def test_deciding_requires_billing_manage(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    request = await submit_request(client, bill_id)
    role = await make_role(client, "Front desk lead", ["admin", "billing.view"])
    await add_account("lead@cedar.example", await hash_password(PASSWORD), role=role)
    client.cookies.clear()
    login = await client.post(
        "/api/auth/login", json={"email": "lead@cedar.example", "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    mode = await client.post("/api/auth/mode", json={"mode": "admin", "password": PASSWORD})
    assert mode.status_code == 200, mode.text

    decided = await client.post(decision_url(bill_id, request["id"]), json={"decision": "approved"})

    assert decided.status_code == 403, decided.text
    assert decided.json()["code"] == "capability_required"


async def test_deciding_requires_admin_mode(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    request = await submit_request(client, bill_id)
    client.cookies.clear()
    login = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.text  # Staff Mode, no /auth/mode call

    decided = await client.post(decision_url(bill_id, request["id"]), json={"decision": "approved"})

    assert decided.status_code == 403, decided.text
    assert decided.json()["code"] == "admin_mode_required"


# --- (b) inline admin edit ------------------------------------------------------------------


async def test_wrong_admin_password_is_refused(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.post(
        inline_url(bill_id, "authenticate"),
        json={"email": EMAIL, "password": "not the password"},
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "invalid_password"


async def test_correct_admin_password_grants_a_window(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.post(
        inline_url(bill_id, "authenticate"), json={"email": EMAIL, "password": PASSWORD}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["active"] is True
    assert resp.json()["admin_email"] == EMAIL
    assert resp.json()["hard_limit_at"] is not None

    status = await client.get(inline_url(bill_id))
    assert status.status_code == 200, status.text
    assert status.json()["active"] is True


async def test_an_account_without_billing_manage_is_refused_even_with_the_right_password(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    role = await make_role(client, "Junior", ["billing.view"])
    await add_account("junior@cedar.example", await hash_password(PASSWORD), role=role)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.post(
        inline_url(bill_id, "authenticate"),
        json={"email": "junior@cedar.example", "password": PASSWORD},
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "capability_required"


async def test_inline_edit_is_attributed_to_the_admin_not_the_staff_session(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)
    authed = await client.post(
        inline_url(bill_id, "authenticate"), json={"email": EMAIL, "password": PASSWORD}
    )
    assert authed.status_code == 200, authed.text

    edited = await client.put(
        inline_url(bill_id, "override"), json={"total_cents": 7500, "reason": "Goodwill"}
    )

    assert edited.status_code == 200, edited.text
    assert edited.json()["override_total_cents"] == 7500

    async with session_scope() as db:
        row = (
            await db.execute(
                text(
                    "SELECT actor_user_id FROM audit_events "
                    "WHERE event_type = 'bill.inline_admin_edited' "
                    "ORDER BY occurred_at DESC LIMIT 1"
                )
            )
        ).one()
        admin_id = await db.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": EMAIL})
        staff_id = await db.scalar(
            text("SELECT id FROM users WHERE email = :e"), {"e": STAFF_EMAIL}
        )
    assert str(row.actor_user_id) == str(admin_id)
    assert str(row.actor_user_id) != str(staff_id)


async def test_editing_without_authenticating_first_is_refused(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.put(
        inline_url(bill_id, "override"), json={"total_cents": 7500, "reason": "x"}
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "inline_admin_authority_required"


async def test_authority_ends_after_one_save(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)
    await client.post(
        inline_url(bill_id, "authenticate"), json={"email": EMAIL, "password": PASSWORD}
    )

    first = await client.put(
        inline_url(bill_id, "override"), json={"total_cents": 7000, "reason": "a"}
    )
    assert first.status_code == 200, first.text

    second = await client.put(
        inline_url(bill_id, "override"), json={"total_cents": 6000, "reason": "b"}
    )

    assert second.status_code == 403, second.text
    assert second.json()["code"] == "inline_admin_authority_required"


async def test_authority_ends_on_release(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)
    await client.post(
        inline_url(bill_id, "authenticate"), json={"email": EMAIL, "password": PASSWORD}
    )

    released = await client.post(inline_url(bill_id, "release"), json={})
    assert released.status_code == 204, released.text

    resp = await client.put(
        inline_url(bill_id, "override"), json={"total_cents": 7000, "reason": "x"}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "inline_admin_authority_required"


async def test_authority_ends_when_the_window_is_gone(client):
    """The same technique `tests/test_modes.py` uses for Admin Mode: move the Redis state
    directly rather than waiting out a 15-minute window."""
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)
    await client.post(
        inline_url(bill_id, "authenticate"), json={"email": EMAIL, "password": PASSWORD}
    )
    await get_redis().delete(_inline_admin_key(bill_id))

    resp = await client.put(
        inline_url(bill_id, "override"), json={"total_cents": 7000, "reason": "x"}
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "inline_admin_authority_required"


async def test_the_window_really_expires_on_its_own(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)
    await client.post(
        inline_url(bill_id, "authenticate"), json={"email": EMAIL, "password": PASSWORD}
    )
    await get_redis().pexpire(_inline_admin_key(bill_id), 50)

    await asyncio.sleep(0.2)

    status = await client.get(inline_url(bill_id))
    assert status.json()["active"] is False
    resp = await client.put(
        inline_url(bill_id, "override"), json={"total_cents": 7000, "reason": "x"}
    )
    assert resp.status_code == 403, resp.text


async def test_authenticating_is_refused_with_the_toggle_off(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    await set_toggle("enable_inline_admin_bill_edit", False)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    resp = await client.post(
        inline_url(bill_id, "authenticate"), json={"email": EMAIL, "password": PASSWORD}
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "inline_admin_billing_disabled"


async def test_disabling_both_toggles_never_removes_the_admins_own_billing_access(client):
    await as_admin(client)
    bill_id, _ = await complete_a_visit(client)
    await set_toggle("enable_bill_override_requests", False)
    await set_toggle("enable_inline_admin_bill_edit", False)

    read = await client.get(f"{BILLS}/{bill_id}")
    assert read.status_code == 200, read.text
    applied = await client.put(f"{BILLS}/{bill_id}/discounts", json={"discount_ids": []})
    assert applied.status_code == 200, applied.text


async def test_inline_admin_requires_a_valid_totp_when_the_admin_is_enrolled(client):
    await as_admin(client)
    secret = await enrol_mfa(client)
    bill_id, _ = await complete_a_visit(client)
    await add_front_desk_account()
    client.cookies.clear()
    await as_staff(client)

    without_code = await client.post(
        inline_url(bill_id, "authenticate"), json={"email": EMAIL, "password": PASSWORD}
    )
    assert without_code.status_code == 403, without_code.text
    assert without_code.json()["code"] == "mfa_required"

    with_code = await client.post(
        inline_url(bill_id, "authenticate"),
        json={"email": EMAIL, "password": PASSWORD, "totp": totp_code(secret)},
    )
    assert with_code.status_code == 200, with_code.text
    assert with_code.json()["active"] is True
