"""S1: the access-log CSV export (#87), through the shared export mechanism (#86) —
`POST/GET/GET .csv .../access-log/exports`, `audit.view`, Admin Mode only, same as the
on-screen report `tests/test_access_report.py` proves. What is worth pinning here: the
export's rows match the on-screen report's rows for the same range, newest first; requesting
one is not itself an access and writes no access-log row, only the shared
`report.export_requested` audit event; poll and download resolve only `kind="access_log"`
exports scoped to the customer in the path; and the 7-day expiry (410) already proven generically
in `tests/test_commission_report.py` applies here too.
"""

import csv
import io

from sqlalchemy import text

from core.db import session_scope
from tests.test_access_log import (  # noqa: F401 — the autouse fixtures come along
    access_rows,
    add_role,
    clean_access_log,
)
from tests.test_access_report import days_ago, display_name, insert_row
from tests.test_appointments import (  # noqa: F401 — the autouse fixture comes along
    CUSTOMERS,
    EMAIL,
    OTHER_PASSWORD,
    PASSWORD,
    as_admin,
    as_staff,
    claimed_instance,
    make_customer,
)

DESK = "desk@cedar.example"


def exports_url(customer_id: str) -> str:
    return f"/api/admin/customers/{customer_id}/access-log/exports"


async def test_the_export_matches_the_on_screen_report_newest_first(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    assert (await client.get(f"{CUSTOMERS}/{customer_id}")).status_code == 200
    await add_role(client, "Front desk", ["customers.view"], DESK)
    client.cookies.clear()
    await as_staff(client, DESK, OTHER_PASSWORD)
    assert (await client.get(f"{CUSTOMERS}/{customer_id}")).status_code == 200
    client.cookies.clear()
    await as_admin(client)
    onscreen = (await client.get(f"/api/admin/customers/{customer_id}/access-log")).json()

    queued = await client.post(exports_url(customer_id), json={})
    assert queued.status_code == 202, queued.text
    # Eager Celery (the suite) has already run the job by the time the 202 is read.
    polled = await client.get(f"{exports_url(customer_id)}/{queued.json()['id']}")
    assert polled.status_code == 200, polled.text
    assert polled.json()["status"] == "ready"
    download = await client.get(polled.json()["download_url"])
    assert download.status_code == 200, download.text
    assert download.headers["content-type"].startswith("text/csv")
    assert "attachment" in download.headers["content-disposition"]

    rows = list(csv.reader(io.StringIO(download.text)))
    assert rows[0] == [
        "id",
        "occurred_at",
        "actor_user_id",
        "actor_name",
        "actor_role",
        "resource_type",
        "resource_id",
        "action",
        "ip",
    ]
    assert len(rows) == 1 + len(onscreen["entries"])
    exported = [dict(zip(rows[0], row, strict=True)) for row in rows[1:]]
    assert [e["actor_name"] for e in exported] == [e["actor_name"] for e in onscreen["entries"]]
    assert [e["actor_role"] for e in exported] == [e["actor_role"] for e in onscreen["entries"]]
    # Newest first, exactly the on-screen order.
    assert exported[0]["occurred_at"] > exported[1]["occurred_at"]
    assert [e["actor_name"] for e in exported] == [
        await display_name(DESK),
        await display_name(EMAIL),
    ]


async def test_the_request_only_enqueues_the_export(client, monkeypatch):
    from customers import access_report

    queued_ids: list[str] = []
    monkeypatch.setattr(access_report.build_report_export, "delay", queued_ids.append)
    await as_admin(client)
    customer_id = await make_customer(client)

    queued = await client.post(exports_url(customer_id), json={})

    assert queued.status_code == 202, queued.text
    assert queued_ids == [queued.json()["id"]]
    assert (queued.json()["status"], queued.json()["download_url"]) == ("pending", None)
    refused = await client.get(f"{exports_url(customer_id)}/{queued_ids[0]}/csv")
    assert refused.status_code == 409, refused.text


async def test_requesting_an_export_is_not_itself_an_access_and_writes_no_row(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    logged = len(await access_rows())

    queued = await client.post(exports_url(customer_id), json={})

    assert queued.status_code == 202, queued.text
    assert len(await access_rows()) == logged


async def test_the_export_request_writes_one_audit_event_with_kind_and_params_no_content(client):
    await as_admin(client)
    customer_id = await make_customer(client)

    queued = await client.post(exports_url(customer_id), json={})
    assert queued.status_code == 202, queued.text

    async with session_scope() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT actor_user_id, target_id, metadata FROM audit_events "
                    "WHERE event_type = 'report.export_requested'"
                )
            )
        ).all()
    assert len(rows) == 1
    _actor_user_id, target_id, metadata = rows[0]
    assert target_id == queued.json()["id"]
    assert metadata == {
        "kind": "access_log",
        "params": {
            "customer_id": customer_id,
            "from": queued.json()["from"],
            "to": queued.json()["to"],
        },
    }
    assert "content" not in metadata


async def test_an_inverted_range_is_refused_before_queueing(client):
    await as_admin(client)
    customer_id = await make_customer(client)

    resp = await client.post(
        exports_url(customer_id), json={"from": "2026-05-02", "to": "2026-05-01"}
    )

    assert resp.status_code == 422, resp.text


async def test_the_default_range_matches_the_on_screen_report(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    for n in (5, 40, 200):
        await insert_row(customer_id, days_ago(n))

    queued = await client.post(exports_url(customer_id), json={})

    assert queued.status_code == 202, queued.text
    onscreen = (await client.get(f"/api/admin/customers/{customer_id}/access-log")).json()
    assert (queued.json()["from"], queued.json()["to"]) == (onscreen["from"], onscreen["to"])
    download = await client.get(
        (await client.get(f"{exports_url(customer_id)}/{queued.json()['id']}")).json()[
            "download_url"
        ]
    )
    rows = list(csv.reader(io.StringIO(download.text)))
    assert len(rows) - 1 == onscreen["total"] == 2  # the 200-days-ago row falls outside 90 days


# --- who may read it ------------------------------------------------------------------------


async def test_the_export_routes_require_audit_view_and_admin_mode(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    client.cookies.clear()
    await as_staff(client, EMAIL, PASSWORD)

    refused_post = await client.post(exports_url(customer_id), json={})
    assert refused_post.status_code == 403, refused_post.text
    assert refused_post.json()["code"] == "admin_mode_required"

    refused_get = await client.get(f"{exports_url(customer_id)}/{customer_id}")
    assert refused_get.status_code in (403, 422), refused_get.text


async def test_admin_mode_without_audit_view_is_refused(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    await add_role(client, "Office", ["admin", "customers.view"], DESK)
    client.cookies.clear()
    await as_admin(client, DESK, OTHER_PASSWORD)

    refused = await client.post(exports_url(customer_id), json={})

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "capability_required"


# --- scoping: only this customer's access_log exports resolve --------------------------------


async def test_poll_and_download_are_scoped_to_the_customer_in_the_path(client):
    await as_admin(client)
    customer_id = await make_customer(client)
    other_id = await make_customer(client)

    queued = await client.post(exports_url(customer_id), json={})
    export_id = queued.json()["id"]

    # Somebody else's export id, under the right customer's path, does not exist there.
    wrong_customer = await client.get(f"{exports_url(other_id)}/{export_id}")
    assert wrong_customer.status_code == 404, wrong_customer.text
    wrong_customer_csv = await client.get(f"{exports_url(other_id)}/{export_id}/csv")
    assert wrong_customer_csv.status_code == 404, wrong_customer_csv.text


async def test_a_commission_export_id_does_not_resolve_here(client):
    """The two kinds share one table; `_load_export` must not resolve the other's row."""
    await as_admin(client)
    customer_id = await make_customer(client)
    commission_queued = await client.post("/api/admin/reports/commission/exports", json={})
    assert commission_queued.status_code == 202, commission_queued.text

    resp = await client.get(f"{exports_url(customer_id)}/{commission_queued.json()['id']}")

    assert resp.status_code == 404, resp.text
