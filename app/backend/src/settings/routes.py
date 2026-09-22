"""This deployment's own identity: profile, timezone, branding — and who may change it.

Moved out of `core/` by this task. `core` holds the `Business` *record*, which every domain
reads; the screens that write it are a domain of their own, and this is it.

**Three different audiences on one router prefix.** Everything under `/api/admin/business`
needs the `admin` capability, which the registry marks administrative — so it is refused to a
role that does not hold it *and* to a session being served in Staff Mode. Everything under
`/api/branding` is anonymous on purpose: the login screen, the browser tab and the setup
wizard are branded before anybody has signed in.

**The timezone is not part of the profile PUT.** It has its own request because it is not the
same kind of edit: every recurring availability rule in the application is a wall-clock time
read against it, so changing it re-interprets data that already exists. A field on a form
somebody is editing to fix a typo in the postal code is the wrong place for that.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from pydantic import AfterValidator, BaseModel, BeforeValidator, EmailStr, Field
from sqlalchemy import select

from auth.capabilities import Requires
from auth.models import User
from core.audit import record_event
from core.db import SessionDep
from core.models import Business
from scheduling import cache
from settings import assets, branding, images
from settings.models import FAVICON, LOGO, BrandingAsset
from settings.timezones import PROVINCES, is_canonical

AdminCapability = Annotated[User, Depends(Requires("admin"))]

router = APIRouter(prefix="/admin", tags=["settings"], dependencies=[Depends(Requires("admin"))])
public = APIRouter(prefix="/branding", tags=["branding"])


# --- field types ----------------------------------------------------------------------------
#
# A text input that has been cleared sends `""`, and an empty string is not a shorter address
# line — it is the absence of one. Normalising here rather than per handler is what keeps
# `""` out of the column, where it would print as a blank line on a receipt.


def _blank_to_none(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


def _stripped(value: str | None) -> str | None:
    return value.strip() if value else value


def _valid_province(value: str | None) -> str | None:
    if value is not None and value.upper() not in PROVINCES:
        raise ValueError("not a Canadian province or territory code")
    return value.upper() if value else value


def _valid_postal_code(value: str | None) -> str | None:
    """Canada Post's `A1A 1A1`, stored in that one shape whatever was typed."""
    if value is None:
        return None
    compact = value.replace(" ", "").upper()
    if len(compact) != 6 or not all(
        (c.isalpha() if i % 2 == 0 else c.isdigit()) for i, c in enumerate(compact)
    ):
        raise ValueError("not a Canadian postal code")
    return f"{compact[:3]} {compact[3:]}"


def _valid_hex(value: str) -> str:
    if not branding.is_hex(value):
        raise ValueError("not a six-digit hex colour, like #1d4ed8")
    return branding.normalise(value)


def _five_minute_step(value: int) -> int:
    if value % 5:
        raise ValueError("the grid goes in 5-minute steps")
    return value


def _canonical_zone(value: str) -> str:
    if not is_canonical(value):
        raise ValueError("not a current IANA timezone name")
    return value


def _text(max_length: int):
    """An optional free-text field: blank means absent, and it is stored trimmed."""
    # The length applies to the string, not to the union: hung off `str | None` it is
    # checked against `None` too, which is a TypeError rather than a validation error.
    return Annotated[
        Annotated[str, Field(max_length=max_length)] | None,
        BeforeValidator(_blank_to_none),
        AfterValidator(_stripped),
    ]


Hex = Annotated[str, AfterValidator(_valid_hex)]
Province = Annotated[str | None, BeforeValidator(_blank_to_none), AfterValidator(_valid_province)]
PostalCode = Annotated[
    str | None, BeforeValidator(_blank_to_none), AfterValidator(_valid_postal_code)
]


# --- the profile ----------------------------------------------------------------------------


class BusinessProfile(BaseModel):
    """What a receipt, an invoice and a form header are rendered from.

    The tax numbers are labelled free text. Their formats differ per jurisdiction and change;
    a pattern that refused a valid one would be unfixable from the screen, and a business that
    mistypes its own GST number finds out on its first invoice either way.
    """

    name: str = Field(min_length=1, max_length=200)
    address_line1: _text(200) = None  # type: ignore[valid-type]
    address_line2: _text(200) = None  # type: ignore[valid-type]
    city: _text(100) = None  # type: ignore[valid-type]
    province: Province = None
    postal_code: PostalCode = None
    phone: _text(32) = None  # type: ignore[valid-type]
    email: Annotated[EmailStr | None, BeforeValidator(_blank_to_none)] = None
    gst_hst_number: _text(64) = None  # type: ignore[valid-type]
    pst_qst_number: _text(64) = None  # type: ignore[valid-type]
    currency_symbol: str = Field(default="$", min_length=1, max_length=8)
    receipt_footer: _text(2000) = None  # type: ignore[valid-type]
    # The two numbers the availability engine reads (tech-stack §19). On the profile rather
    # than on a screen of their own because they are settings a business sets once, and the
    # matching CHECK constraints are in migration 0013.
    slot_granularity_minutes: Annotated[
        int, Field(ge=5, le=60), AfterValidator(_five_minute_step)
    ] = 15
    booking_horizon_days: Annotated[int, Field(ge=1, le=365)] = 90
    # Classification (pre-flight D8): a client becomes "vip" at this many `completed`
    # appointments. The CHECK on `businesses` (migration 0020) refuses the same range at the
    # database; this is the 422 a person sees first.
    vip_visit_threshold: Annotated[int, Field(ge=2, le=1000)] = 10


class BusinessOut(BaseModel):
    """The same fields, read back — and deliberately *not* a subclass of the one above.

    Inheriting would re-run the input validators against whatever is in the row, so a value
    written out of band (a data fix in `psql`, a future import) would make the read fail with
    a 500. A read model's job is to report what is there, not to refuse it: the constraints in
    migration 0008 and `BusinessProfile` are what keep bad values out, at the moment they are
    written, where the answer can be a 422 somebody can act on.
    """

    name: str
    address_line1: str | None
    address_line2: str | None
    city: str | None
    province: str | None
    postal_code: str | None
    phone: str | None
    email: str | None
    gst_hst_number: str | None
    pst_qst_number: str | None
    currency_symbol: str
    receipt_footer: str | None
    slot_granularity_minutes: int
    booking_horizon_days: int
    vip_visit_threshold: int
    # Read-only here on purpose: the country is fixed, the timezone has its own endpoint, and
    # `setup_completed_at` is written once by the wizard and never again.
    country: str
    timezone: str
    setup_completed_at: datetime | None


@router.get("/business")
async def read_business(db: SessionDep) -> BusinessOut:
    business = await _business(db)
    return BusinessOut.model_validate(business, from_attributes=True)


@router.put("/business")
async def update_business(
    payload: BusinessProfile, admin: AdminCapability, db: SessionDep
) -> BusinessOut:
    business = await _business(db)
    changed = [
        field for field, value in payload.model_dump().items() if getattr(business, field) != value
    ]
    for field, value in payload.model_dump().items():
        setattr(business, field, value)
    if changed:
        # Field names, not values. Which fields an administrator touched is the fact an audit
        # needs; copying the address in would put the same data in a second table with a
        # different retention horizon, for no question it helps answer.
        record_event(
            db,
            "business.profile_updated",
            target_type="business",
            target_id=str(business.id),
            actor_user_id=admin.id,
            metadata={"changed": changed},
        )
    await db.commit()
    if changed:
        # Only `slot_granularity_minutes` and `booking_horizon_days` are engine inputs, but
        # `changed` is field names, not which of them fired — bumping on any profile edit is
        # the cheap side to be wrong on, and this screen is not saved often.
        await cache.bump()
    return BusinessOut.model_validate(business, from_attributes=True)


# --- the timezone ---------------------------------------------------------------------------


class TimezoneChange(BaseModel):
    timezone: Annotated[str, AfterValidator(_canonical_zone)]


@router.patch("/business/timezone")
async def update_timezone(
    payload: TimezoneChange, admin: AdminCapability, db: SessionDep
) -> TimezoneChange:
    """Audited old → new, because it re-reads data rather than replacing it.

    Appointment instants are `timestamptz` and do not move. Recurring availability — the
    weekly matrix, split shifts, cancellation windows — is stored as local wall-clock times,
    so "Mondays 9:00" stays 9:00 and now means 9:00 in the new zone. That is the intended
    behaviour and the confirmation dialog says so in as many words; this entry is how anybody
    looking at a schedule that shifted finds out when, and by whom.
    """
    business = await _business(db)
    was = business.timezone
    business.timezone = payload.timezone
    if was != payload.timezone:
        record_event(
            db,
            "business.timezone_changed",
            target_type="business",
            target_id=str(business.id),
            actor_user_id=admin.id,
            metadata={"timezone": [was, payload.timezone]},
        )
    await db.commit()
    if was != payload.timezone:
        # Every recurring rule the engine reads is a wall-clock time against this zone.
        await cache.bump()
    return payload


class ProvinceOut(BaseModel):
    code: str
    name: str
    # The zone *most* of this province keeps. A suggestion: Saskatchewan skips daylight
    # saving, the BC Kootenays are Mountain, northwestern Ontario is Central and Labrador
    # spans two zones, so nothing here is applied without somebody confirming it.
    timezone: str


@router.get("/business/provinces")
async def provinces() -> list[ProvinceOut]:
    """The select's options and the timezone suggestion, from the one table that holds both."""
    return [
        ProvinceOut(code=code, name=name, timezone=zone)
        for code, (name, zone) in sorted(PROVINCES.items(), key=lambda item: item[1][0])
    ]


# --- colours --------------------------------------------------------------------------------


class BrandColours(BaseModel):
    brand_primary: Hex
    brand_secondary: Hex


class BrandingOut(BrandColours):
    colors: dict[str, str]
    # Text on each brand surface, in the theme it is used in. Below 4.5 the screen warns and
    # saves anyway — it is the business's own brand.
    contrast: dict[str, float]


def _branding_out(primary: str, secondary: str) -> BrandingOut:
    computed = branding.palette(primary, secondary)
    return BrandingOut(
        brand_primary=computed.primary,
        brand_secondary=computed.secondary,
        colors=computed.colors(),
        contrast=computed.contrast(),
    )


@router.get("/business/branding")
async def read_branding(db: SessionDep) -> BrandingOut:
    business = await _business(db)
    return _branding_out(business.brand_primary, business.brand_secondary)


@router.get("/business/branding/preview")
async def preview_branding(brand_primary: Hex, brand_secondary: Hex) -> BrandingOut:
    """What the screen draws while somebody is still choosing.

    A round trip rather than a second copy of `settings/branding.py` in TypeScript: two
    implementations of an Oklab conversion is two answers to "what does this look like on
    dark", and the one on screen would be the one that is wrong.
    """
    return _branding_out(brand_primary, brand_secondary)


@router.put("/business/branding")
async def update_branding(
    payload: BrandColours, admin: AdminCapability, db: SessionDep
) -> BrandingOut:
    business = await _business(db)
    # Only when something actually moved, like the profile above it. A screen that saves
    # whatever is in its inputs turns "who changed the brand" into a list of everyone who
    # ever opened the tab and pressed the button.
    changed = {
        field: [getattr(business, field), getattr(payload, field)]
        for field in ("brand_primary", "brand_secondary")
        if getattr(business, field) != getattr(payload, field)
    }
    business.brand_primary = payload.brand_primary
    business.brand_secondary = payload.brand_secondary
    if changed:
        record_event(
            db,
            "business.branding_updated",
            target_type="business",
            target_id=str(business.id),
            actor_user_id=admin.id,
            metadata=changed,
        )
    await db.commit()
    return _branding_out(payload.brand_primary, payload.brand_secondary)


# --- the two images -------------------------------------------------------------------------
#
# Thin by design: the bytes, the caching and the conditional request all live in
# `settings/assets.py`. What is left here is which limits each of the two kinds gets.


@router.post("/business/logo")
async def upload_logo(file: UploadFile, admin: AdminCapability, db: SessionDep) -> assets.AssetOut:
    return await assets.store(
        db,
        admin,
        LOGO,
        file,
        allowed=("png", "jpeg", "webp"),
        max_px=images.LOGO_MAX_PX,
        cap=images.LOGO_MAX_BYTES,
    )


@router.post("/business/favicon")
async def upload_favicon(
    file: UploadFile, admin: AdminCapability, db: SessionDep
) -> assets.AssetOut:
    return await assets.store(
        db,
        admin,
        FAVICON,
        file,
        allowed=("png", "ico"),
        max_px=images.FAVICON_MAX_PX,
        cap=images.FAVICON_MAX_BYTES,
    )


@router.delete("/business/logo", status_code=204)
async def remove_logo(admin: AdminCapability, db: SessionDep) -> Response:
    return await assets.remove(db, admin, LOGO)


@router.delete("/business/favicon", status_code=204)
async def remove_favicon(admin: AdminCapability, db: SessionDep) -> Response:
    return await assets.remove(db, admin, FAVICON)


# --- what the browser reads (no session) ----------------------------------------------------


class BrandingDocument(BaseModel):
    """Everything the shell needs to look like this business, in one anonymous request.

    `timezone` is here because the schedule needs "today" to be the business's today before
    it has made any other request, and the branding document is the one thing every screen
    has already read. A zone name is not a secret."""

    name: str
    timezone: str
    colors: dict[str, str]
    logo_url: str | None
    logo_etag: str | None
    favicon_url: str | None
    favicon_etag: str | None


@public.get("")
async def branding_document(db: SessionDep) -> BrandingDocument:
    business = await db.scalar(select(Business).where(Business.id == 1))
    palette = branding.palette(
        business.brand_primary if business else "#1d4ed8",
        business.brand_secondary if business else "#0f766e",
    )
    digests = dict(
        (await db.execute(select(BrandingAsset.kind, BrandingAsset.sha256))).all()  # type: ignore[arg-type]
    )
    return BrandingDocument(
        # An unclaimed instance has no business row yet, and the setup wizard still has to be
        # called something. The product name is the honest answer until there is another.
        name=business.name if business else "LinSuite",
        timezone=business.timezone if business else "UTC",
        colors=palette.colors(),
        logo_url=assets.url_of(LOGO, digests[LOGO]) if LOGO in digests else None,
        logo_etag=assets.etag_of(digests[LOGO]) if LOGO in digests else None,
        favicon_url=assets.url_of(FAVICON, digests[FAVICON]) if FAVICON in digests else None,
        favicon_etag=assets.etag_of(digests[FAVICON]) if FAVICON in digests else None,
    )


@public.get("/logo")
async def serve_logo(db: SessionDep, request: Request) -> Response:
    return await assets.serve(db, request, LOGO)


@public.get("/favicon")
async def serve_favicon(db: SessionDep, request: Request) -> Response:
    return await assets.serve(db, request, FAVICON)


# --- the two multi-factor switches ----------------------------------------------------------


class SecurityPolicy(BaseModel):
    """The two multi-factor decisions a business gets to make (PRD §1, tech-stack §14).

    One model for reading and writing, because both fields are always sent: a PATCH that
    could carry one of them would need a tri-state per field to tell "leave it" from "turn
    it off", and this screen has two switches on it.
    """

    # On by default. Off is the solo operator with one phone, who is better served by a
    # strong password than by being locked out of their own business.
    mfa_required_for_admin: bool
    # Off by default, and the settings screen says why in as many words: an emailed code is
    # lower assurance, because the inbox usually lives in the same browser an attacker has.
    mfa_email_otp_allowed: bool


@router.get("/business/security")
async def read_security(db: SessionDep) -> SecurityPolicy:
    business = await _business(db)
    return SecurityPolicy(
        mfa_required_for_admin=business.mfa_required_for_admin,
        mfa_email_otp_allowed=business.mfa_email_otp_allowed,
    )


@router.patch("/business/security")
async def update_security(
    payload: SecurityPolicy, admin: AdminCapability, db: SessionDep
) -> SecurityPolicy:
    """Turning either of these off weakens the instance, so both are audited with a name."""
    business = await _business(db)
    was = (business.mfa_required_for_admin, business.mfa_email_otp_allowed)
    business.mfa_required_for_admin = payload.mfa_required_for_admin
    business.mfa_email_otp_allowed = payload.mfa_email_otp_allowed
    record_event(
        db,
        "business.security_changed",
        target_type="business",
        target_id=str(business.id),
        actor_user_id=admin.id,
        metadata={
            "mfa_required_for_admin": [was[0], payload.mfa_required_for_admin],
            "mfa_email_otp_allowed": [was[1], payload.mfa_email_otp_allowed],
        },
    )
    await db.commit()
    return payload


# --- shared ---------------------------------------------------------------------------------


async def _business(db: SessionDep) -> Business:
    business = await db.scalar(select(Business).where(Business.id == 1))
    if business is None:
        # Unreachable through the UI: an unclaimed instance has no accounts to sign in with.
        raise HTTPException(status_code=404, detail="This instance has not been set up.")
    return business
