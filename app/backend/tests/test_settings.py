"""S1: the business's own settings — profile, timezone, branding.

Three rules this file exists to hold down.

**The profile is validated at the boundary.** A postal code, a province and a brand colour are
the three fields a person can get almost-right, and "almost" is what ends up printed on a
receipt. Each is refused with a 422 rather than stored and rendered later.

**The timezone is a human decision.** The province *suggests* a zone and nothing applies it;
the suggestion endpoint is a lookup, the change is a separate request, and the change is
audited old → new because every recurring wall-clock rule in the app is read against it.

**Uploads are the one hole in the JSON-only CSRF guard.** Exactly two paths accept multipart,
they are named in `main.py`, and on those two the `Origin` has to match this deployment.
Everywhere else multipart is still a 415, which is what
`test_multipart_is_still_refused_on_every_other_path` is here to keep true.
"""

import hashlib
import io

import pytest
from PIL import Image
from sqlalchemy import text

from core.db import get_purge_engine, session_scope
from core.redis import get_redis
from core.security import hash_password

EMAIL = "owner@cedar.example"
PASSWORD = "correct horse battery"
STAFF_PASSWORD = "several unrelated words"
ORIGIN = "http://test.linsuite.example"  # conftest's APP_BASE_URL

SETUP = {
    "business_name": "Cedar Lane Clinic",
    "timezone": "America/Toronto",
    "admin_email": EMAIL,
    "admin_password": PASSWORD,
}

BUSINESS = "/api/admin/business"
BRANDING = "/api/branding"

PROFILE = {
    "name": "Cedar Lane Clinic",
    "address_line1": "1200 Cedar Lane",
    "address_line2": "Suite 4",
    "city": "Victoria",
    "province": "BC",
    "postal_code": "V8W 1P6",
    "phone": "250-555-0134",
    "email": "hello@cedar.example",
    "gst_hst_number": "123456789 RT0001",
    "pst_qst_number": "PST-1234-5678",
    "currency_symbol": "$",
    "receipt_footer": "Thank you. Massage therapy is HST exempt.",
}


@pytest.fixture(autouse=True)
async def claimed_instance(client):
    async with get_purge_engine().begin() as purge:
        await purge.execute(text("DELETE FROM audit_events"))
    async with session_scope() as db:
        for table in ("branding_assets", "users", "businesses", "setup_token"):
            await db.execute(text(f"DELETE FROM {table}"))
        await db.execute(text("DELETE FROM roles WHERE NOT is_system"))
        await db.commit()
    await get_redis().flushdb()

    from auth import setup

    async with session_scope() as db:
        token = await setup.bootstrap_setup_token(db)
    resp = await client.post("/api/setup", json={**SETUP, "token": token})
    assert resp.status_code == 201, resp.text
    # This suite is about settings, not about the second factor; the enrolment gate would
    # otherwise stand between every test and the screen under test.
    async with session_scope() as db:
        await db.execute(text("UPDATE businesses SET mfa_required_for_admin = false"))
        await db.commit()
    client.cookies.clear()
    yield


# --- helpers --------------------------------------------------------------------------------


async def as_admin(client, email=EMAIL, password=PASSWORD):
    resp = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    resp = await client.post("/api/auth/mode", json={"mode": "admin", "password": password})
    assert resp.status_code == 200, resp.text


async def put_profile(client, **overrides):
    return await client.put(BUSINESS, json={**PROFILE, **overrides})


async def audit(event_type: str) -> list[str]:
    async with session_scope() as db:
        rows = await db.execute(
            text(
                "SELECT metadata::text FROM audit_events WHERE event_type = :t "
                "ORDER BY occurred_at, id"
            ),
            {"t": event_type},
        )
    return [row[0] for row in rows]


def png(width: int, height: int = 0, colour=(29, 78, 216)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height or width), colour).save(buffer, "PNG")
    return buffer.getvalue()


def upload(
    client,
    path: str,
    data: bytes,
    name: str = "logo.png",
    content_type: str = "image/png",
    origin: str | None = ORIGIN,
):
    headers = {"Origin": origin} if origin else {}
    return client.post(path, files={"file": (name, data, content_type)}, headers=headers)


# --- the profile ----------------------------------------------------------------------------


async def test_the_profile_round_trips(client):
    await as_admin(client)

    saved = await put_profile(client)
    assert saved.status_code == 200, saved.text

    read = await client.get(BUSINESS)
    assert read.status_code == 200, read.text
    body = read.json()
    for field, value in PROFILE.items():
        assert body[field] == value
    # Country is fixed and the timezone is not this endpoint's to change.
    assert body["country"] == "CA"
    assert body["timezone"] == "America/Toronto"


async def test_only_the_fields_that_changed_are_audited(client):
    await as_admin(client)
    await put_profile(client)

    await put_profile(client, city="Saanich")

    assert len(await audit("business.profile_updated")) == 2
    last = (await audit("business.profile_updated"))[-1]
    assert "city" in last
    assert "postal_code" not in last


@pytest.mark.parametrize(
    "field,value",
    [
        ("postal_code", "90210"),
        ("postal_code", "VV8 1P6"),
        ("province", "XX"),
        ("province", "British Columbia"),
        ("email", "not-an-address"),
        ("name", ""),
    ],
)
async def test_a_field_the_receipt_would_print_wrong_is_refused(client, field, value):
    await as_admin(client)

    resp = await put_profile(client, **{field: value})

    assert resp.status_code == 422, resp.text


async def test_a_postal_code_is_stored_in_one_shape(client):
    await as_admin(client)

    await put_profile(client, postal_code="v8w1p6")

    assert (await client.get(BUSINESS)).json()["postal_code"] == "V8W 1P6"


# --- the timezone ---------------------------------------------------------------------------


async def test_changing_the_timezone_is_audited_old_to_new(client):
    await as_admin(client)

    resp = await client.patch(f"{BUSINESS}/timezone", json={"timezone": "America/Vancouver"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["timezone"] == "America/Vancouver"
    assert (await client.get(BUSINESS)).json()["timezone"] == "America/Vancouver"
    entries = await audit("business.timezone_changed")
    assert len(entries) == 1
    assert "America/Toronto" in entries[0] and "America/Vancouver" in entries[0]


@pytest.mark.parametrize("zone", ["US/Eastern", "Asia/Calcutta", "Africa/Asmera", "Mars/Olympus"])
async def test_a_deprecated_or_unknown_zone_is_refused(client, zone):
    await as_admin(client)

    resp = await client.patch(f"{BUSINESS}/timezone", json={"timezone": zone})

    assert resp.status_code == 422, resp.text


async def test_the_timezone_list_is_canonical(client):
    resp = await client.get("/api/setup/timezones")

    zones = resp.json()["timezones"]
    assert "America/Toronto" in zones
    assert "America/St_Johns" in zones
    for alias in ("US/Eastern", "Asia/Calcutta", "Africa/Asmera", "Canada/Eastern", "EST"):
        assert alias not in zones


@pytest.mark.parametrize(
    "province,zone",
    [
        ("BC", "America/Vancouver"),
        ("AB", "America/Edmonton"),
        ("SK", "America/Regina"),
        ("MB", "America/Winnipeg"),
        ("ON", "America/Toronto"),
        ("QC", "America/Toronto"),
        ("NB", "America/Halifax"),
        ("NS", "America/Halifax"),
        ("PE", "America/Halifax"),
        ("NL", "America/St_Johns"),
        ("YT", "America/Whitehorse"),
        # Yellowknife became a link to Edmonton in tzdata 2023a, so the canonical answer for
        # the Northwest Territories is Edmonton — same clock, a name that is not deprecated.
        ("NT", "America/Edmonton"),
        ("NU", "America/Iqaluit"),
    ],
)
async def test_a_province_suggests_the_zone_it_is_usually_in(client, province, zone):
    await as_admin(client)

    resp = await client.get(f"{BUSINESS}/provinces")

    assert resp.status_code == 200, resp.text
    suggestions = {row["code"]: row["timezone"] for row in resp.json()}
    assert len(suggestions) == 13
    assert suggestions[province] == zone
    # Every suggestion has to be a zone the change endpoint would accept.
    assert zone in (await client.get("/api/setup/timezones")).json()["timezones"]


async def test_a_suggestion_is_only_a_suggestion(client):
    """Reading the map never changes anything: a human confirms, in a separate request."""
    await as_admin(client)

    await client.get(f"{BUSINESS}/provinces")

    assert (await client.get(BUSINESS)).json()["timezone"] == "America/Toronto"
    assert await audit("business.timezone_changed") == []


# --- colours --------------------------------------------------------------------------------


async def test_branding_is_public_and_carries_the_derived_dark_variants(client):
    await as_admin(client)
    saved = await client.put(
        f"{BUSINESS}/branding", json={"brand_primary": "#1d4ed8", "brand_secondary": "#0f766e"}
    )
    assert saved.status_code == 200, saved.text

    client.cookies.clear()
    resp = await client.get(BRANDING)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Cedar Lane Clinic"
    colours = body["colors"]
    assert colours["primary"] == "#1d4ed8"
    assert colours["secondary"] == "#0f766e"
    # Lightened toward white, so it stays legible on slate-950.
    assert colours["primary_dark"] != colours["primary"]
    assert int(colours["primary_dark"][1:3], 16) > int(colours["primary"][1:3], 16)
    assert body["logo_url"] is None and body["favicon_url"] is None


@pytest.mark.parametrize("value", ["1d4ed8", "#1d4ed", "#nothex", "blue", "#1D4ED8FF"])
async def test_a_colour_that_is_not_a_six_digit_hex_is_refused(client, value):
    await as_admin(client)

    resp = await client.put(
        f"{BUSINESS}/branding", json={"brand_primary": value, "brand_secondary": "#0f766e"}
    )

    assert resp.status_code == 422, resp.text


async def test_the_preview_reports_the_contrast_of_text_on_each_colour(client):
    await as_admin(client)

    resp = await client.get(
        f"{BUSINESS}/branding/preview",
        params={"brand_primary": "#1d4ed8", "brand_secondary": "#facc15"},
    )

    assert resp.status_code == 200, resp.text
    contrast = resp.json()["contrast"]
    # White on a deep blue is comfortable; near-black on amber-400 is too, and both are the
    # *better* of the two foregrounds — which is what the screen warns about when it is < 4.5.
    assert contrast["primary"] > 4.5
    assert contrast["secondary"] > 4.5


async def test_a_low_contrast_colour_is_still_the_business_s_to_choose(client):
    await as_admin(client)

    resp = await client.put(
        f"{BUSINESS}/branding", json={"brand_primary": "#7a7a7a", "brand_secondary": "#0f766e"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["contrast"]["primary"] < 4.5


async def test_changing_the_colours_is_audited(client):
    await as_admin(client)

    await client.put(
        f"{BUSINESS}/branding", json={"brand_primary": "#aa0000", "brand_secondary": "#00aa00"}
    )

    entries = await audit("business.branding_updated")
    assert len(entries) == 1
    assert "#aa0000" in entries[0]


# --- uploads --------------------------------------------------------------------------------


async def test_a_logo_upload_is_accepted_on_the_allowlisted_path(client):
    await as_admin(client)

    resp = await upload(client, f"{BUSINESS}/logo", png(240))

    assert resp.status_code == 200, resp.text
    assert resp.json()["byte_length"] > 0

    client.cookies.clear()
    served = await client.get(f"{BRANDING}/logo")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    assert served.headers["etag"]


async def test_a_multipart_upload_without_a_matching_origin_is_refused(client):
    await as_admin(client)

    assert (await upload(client, f"{BUSINESS}/logo", png(64), origin=None)).status_code == 403
    assert (
        await upload(client, f"{BUSINESS}/logo", png(64), origin="http://evil.example")
    ).status_code == 403


async def test_multipart_is_still_refused_on_every_other_path(client):
    await as_admin(client)

    resp = await client.post(
        "/api/admin/roles",
        files={"file": ("role.png", png(8), "image/png")},
        headers={"Origin": ORIGIN},
    )

    assert resp.status_code == 415, resp.text


async def test_an_oversized_logo_is_refused(client):
    await as_admin(client)

    # Noise, so PNG cannot compress it under the cap.
    import os

    big = io.BytesIO()
    Image.frombytes("RGB", (1200, 1200), os.urandom(1200 * 1200 * 3)).save(big, "PNG")
    assert len(big.getvalue()) > 1024 * 1024

    resp = await upload(client, f"{BUSINESS}/logo", big.getvalue())

    assert resp.status_code == 413, resp.text


@pytest.mark.parametrize(
    "name,content_type,data",
    [
        ("logo.svg", "image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'),
        # A PNG extension and content type do not make it a PNG.
        ("logo.png", "image/png", b"GIF89a" + b"\x00" * 64),
    ],
)
async def test_a_file_that_is_not_a_permitted_image_is_refused(client, name, content_type, data):
    await as_admin(client)

    resp = await upload(client, f"{BUSINESS}/logo", data, name=name, content_type=content_type)

    assert resp.status_code == 415, resp.text


async def test_a_large_logo_comes_back_downscaled_and_re_encoded(client):
    await as_admin(client)

    resp = await upload(client, f"{BUSINESS}/logo", png(2000, 1000))

    assert resp.status_code == 200, resp.text
    assert (resp.json()["width"], resp.json()["height"]) == (512, 256)
    served = await client.get(f"{BRANDING}/logo")
    image = Image.open(io.BytesIO(served.content))
    assert image.format == "PNG"
    assert max(image.size) == 512


async def test_a_favicon_is_re_encoded_small(client):
    await as_admin(client)

    resp = await upload(client, f"{BUSINESS}/favicon", png(512), name="favicon.png")

    assert resp.status_code == 200, resp.text
    served = await client.get(f"{BRANDING}/favicon")
    image = Image.open(io.BytesIO(served.content))
    assert image.format == "PNG"
    assert max(image.size) == 64


async def test_the_public_asset_answers_304_to_a_matching_etag(client):
    await as_admin(client)
    await upload(client, f"{BUSINESS}/logo", png(120))
    client.cookies.clear()

    first = await client.get(f"{BRANDING}/logo")
    etag = first.headers["etag"]
    again = await client.get(f"{BRANDING}/logo", headers={"If-None-Match": etag})

    assert again.status_code == 304
    assert again.content == b""
    assert etag.strip('"') == hashlib.sha256(first.content).hexdigest()
    assert "max-age" in first.headers["cache-control"]


async def test_the_branding_document_points_at_the_assets_once_they_exist(client):
    await as_admin(client)
    await upload(client, f"{BUSINESS}/logo", png(120))
    await upload(client, f"{BUSINESS}/favicon", png(120), name="favicon.png")
    client.cookies.clear()

    body = (await client.get(BRANDING)).json()

    assert body["logo_url"].startswith("/api/branding/logo")
    assert body["favicon_url"].startswith("/api/branding/favicon")
    assert body["logo_etag"] and body["favicon_etag"]


async def test_removing_a_logo_takes_it_out_of_the_branding_document(client):
    await as_admin(client)
    await upload(client, f"{BUSINESS}/logo", png(120))

    resp = await client.delete(f"{BUSINESS}/logo", headers={"Content-Type": "application/json"})

    assert resp.status_code == 204, resp.text
    assert (await client.get(BRANDING)).json()["logo_url"] is None
    assert (await client.get(f"{BRANDING}/logo")).status_code == 404


# --- who may do any of this -----------------------------------------------------------------

WRITES = [
    ("PUT", BUSINESS, PROFILE),
    ("PATCH", f"{BUSINESS}/timezone", {"timezone": "America/Halifax"}),
    ("PUT", f"{BUSINESS}/branding", {"brand_primary": "#aa0000", "brand_secondary": "#00aa00"}),
    ("DELETE", f"{BUSINESS}/logo", None),
]


@pytest.mark.parametrize("method,path,body", WRITES)
async def test_a_write_is_refused_outside_admin_mode(client, method, path, body):
    resp = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 200, resp.text

    answer = await client.request(
        method, path, json=body, headers={"Content-Type": "application/json"}
    )

    assert answer.status_code == 403
    assert answer.json()["code"] == "admin_mode_required"


@pytest.mark.parametrize("method,path,body", WRITES)
async def test_a_role_without_the_admin_capability_is_refused(client, method, path, body):
    await as_admin(client)
    role = await client.post(
        "/api/admin/roles",
        json={"name": "Front desk", "description": "Reception.", "capabilities": ["schedule.view"]},
    )
    assert role.status_code == 201, role.text
    digest = await hash_password(STAFF_PASSWORD)
    async with session_scope() as db:
        await db.execute(
            text("INSERT INTO users (email, password_hash, role_id) VALUES (:e, :h, :r)"),
            {"e": "desk@cedar.example", "h": digest, "r": role.json()["id"]},
        )
        await db.commit()
    client.cookies.clear()
    resp = await client.post(
        "/api/auth/login", json={"email": "desk@cedar.example", "password": STAFF_PASSWORD}
    )
    assert resp.status_code == 200, resp.text

    answer = await client.request(
        method, path, json=body, headers={"Content-Type": "application/json"}
    )

    assert answer.status_code == 403


async def test_an_upload_is_refused_outside_admin_mode(client):
    resp = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 200, resp.text

    answer = await upload(client, f"{BUSINESS}/logo", png(64))

    assert answer.status_code == 403
    assert answer.json()["code"] == "admin_mode_required"


async def test_the_branding_document_is_public(client):
    """The login screen and the browser tab are branded before anybody has signed in."""
    resp = await client.get(BRANDING)

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Cedar Lane Clinic"
