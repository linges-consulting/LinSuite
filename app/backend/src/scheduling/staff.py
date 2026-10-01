"""Settings → Staff: who works here, what they are licensed to do, and what they earn on it.

Everything is `users.manage`, which the registry marks administrative — so an Admin Mode
window is required on top of the capability, and `Requires` applies that without this module
mentioning it.

**Creating a staff member creates the account.** One act, because they are one fact: a person
who works here. The `users` row is born with `password_hash` NULL and an invitation link is
the only way to give it one — no temporary password is ever minted, so there is never a
working credential sitting in a mailbox, a log or an administrator's notes. The invitation
reuses the password-reset token mechanism (`auth/passwords.py`) at a longer expiry, because a
new colleague is often invited on a Friday, and the endpoint that spends it is the one that
already exists.

**Credentials are conditional, and the condition is real.** Insurers reject treatment
receipts that lack a practitioner's designation and licence number (tech-stack §21), so
`is_practitioner` without both is a 422 here and a CHECK constraint underneath. Somebody who
is not a practitioner is asked for neither — a receptionist has no college.

**Deactivation is the only ending.** It ends every session, refuses the next sign-in, and
leaves the row exactly where every appointment, invoice and audit entry expects to find it.
The one deactivation refused is the one that would leave nobody who can administer this
instance — the same guard, and the same reasoning, as demoting the last administrator.
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from auth.admin_users import UserOut, locks_for
from auth.capabilities import Requires
from auth.models import PasswordResetToken, Role, User
from auth.passwords import digest
from auth.roles import assert_an_administrator_remains
from auth.session import revoke_all
from core.audit import record_event
from core.config import get_settings
from core.db import SessionDep
from core.models import Business
from notifications.tasks import send_email
from scheduling import cache
from scheduling._admin_forms import blank_to_none, known_colour, refuse, refuse_emptied_field
from scheduling.models import MAX_BASIS_POINTS, Appointment, Staff
from scheduling.palette import BY_KEY, PALETTE, next_free

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/staff", tags=["staff"])
# The read the schedule makes: who has a column, in what order, in what colour. Its own
# router on the shop-floor prefix for the same reason the catalog has one (`services.py`):
# `/admin` is administration, and drawing the calendar is not.
public = APIRouter(prefix="/staff", tags=["staff"])

StaffManager = Annotated[User, Depends(Requires("users.manage"))]
Scheduler = Annotated[User, Depends(Requires("schedule.view"))]


# --- what goes over the wire ------------------------------------------------------------


class ColourOut(BaseModel):
    key: str
    name: str
    hex: str
    # The same colour on a dark calendar, derived by `settings/branding.py` rather than
    # picked by hand, and the text colour that reads on each.
    dark_hex: str
    foreground: str
    dark_foreground: str


class StaffOut(UserOut):
    """The account facts Task 5 already showed, plus the person behind them.

    One row for the whole screen: the table needs the role, the lock and the second factor
    beside the colour and the credentials, and two endpoints answering about one person would
    be two things to keep in step.

    `id` is the staff member's, not the account's — this is the id every endpoint below takes.
    The account's is `user_id`, which the three Task 5 endpoints (`/role`, `/unlock`,
    `/mfa/reset`) still key on.
    """

    id: str
    user_id: str
    first_name: str
    last_name: str
    display_name: str
    is_practitioner: bool
    designation: str | None
    licence_number: str | None
    commission_rate_services_bp: int
    commission_rate_retail_bp: int
    colour: str
    max_concurrent_appointments: int
    active: bool
    sort_order: int
    # No password has ever been set, so the invitation is still outstanding. What the
    # "Resend invite" action is offered from, and what the row is badged with.
    invite_pending: bool
    # How many confirmed appointments this person still has ahead of them. Only the
    # deactivate response fills it in — it is the one answer that changes what the person
    # clicking is about to do (`/api/schedule` keeps a column for them either way).
    future_appointments: int | None = None


def _out(
    staff: Staff,
    user: User,
    locked_until: datetime | None,
    *,
    future_appointments: int | None = None,
) -> StaffOut:
    return StaffOut(
        # `id` is the staff member's here, so the account's is left out and re-sent as
        # `user_id` — the one field this shape deliberately redefines.
        **UserOut.of(user, locked_until).model_dump(exclude={"id"}),
        id=str(staff.id),
        user_id=str(user.id),
        first_name=staff.first_name,
        last_name=staff.last_name,
        display_name=staff.display_name,
        is_practitioner=staff.is_practitioner,
        designation=staff.designation,
        licence_number=staff.licence_number,
        commission_rate_services_bp=staff.commission_rate_services_bp,
        commission_rate_retail_bp=staff.commission_rate_retail_bp,
        colour=staff.colour,
        max_concurrent_appointments=staff.max_concurrent_appointments,
        active=staff.active,
        sort_order=staff.sort_order,
        invite_pending=user.password_hash is None,
        future_appointments=future_appointments,
    )


# --- what comes in ----------------------------------------------------------------------

Rate = Annotated[int, Field(ge=0, le=MAX_BASIS_POINTS)]
Name = Annotated[str, Field(min_length=1, max_length=100)]


class StaffFields(BaseModel):
    """Everything an administrator sets, on create and on edit alike."""

    first_name: Name
    last_name: Name
    # Absent means "call them by their name", which is the common case and one less field
    # to fill in for it.
    display_name: Annotated[str | None, Field(max_length=200)] = None
    is_practitioner: bool = False
    designation: Annotated[str | None, Field(max_length=64)] = None
    licence_number: Annotated[str | None, Field(max_length=64)] = None
    commission_rate_services_bp: Rate = 0
    commission_rate_retail_bp: Rate = 0
    # Absent on create means "choose one for me", which is what every new staff member gets.
    colour: str | None = None
    max_concurrent_appointments: Annotated[int, Field(ge=1)] = 1
    sort_order: int = 0

    @field_validator("designation", "licence_number", "display_name", mode="after")
    @classmethod
    def _trimmed(cls, value: str | None) -> str | None:
        return blank_to_none(value)

    @field_validator("first_name", "last_name", mode="after")
    @classmethod
    def _real_name(cls, value: str) -> str:
        # `min_length` lets a string of spaces through, and " " is not a name.
        if not value.strip():
            raise ValueError("this cannot be blank")
        return value.strip()

    @field_validator("colour")
    @classmethod
    def _known_colour(cls, value: str | None) -> str | None:
        return known_colour(value)


class StaffCreate(StaffFields):
    email: EmailStr
    role_id: uuid.UUID


# The columns with no "absent" state. `None` on one of these is a request to delete a fact
# the row cannot be without — it would be a NOT NULL violation at commit, and a 500 for the
# caller — so it is refused at the boundary. Every field here is `… | None` only because
# "leave it alone" and "set it" have to be told apart on a PATCH.
_NOT_NULLABLE = (
    "first_name",
    "last_name",
    "is_practitioner",
    "commission_rate_services_bp",
    "commission_rate_retail_bp",
    "colour",
    "max_concurrent_appointments",
    "sort_order",
)


class StaffPatch(BaseModel):
    """Every field optional, and the credentials are checked against the result rather than
    against what was sent: turning `is_practitioner` on without a licence in the same request
    is the mistake this has to catch.

    `designation` and `licence_number` are the only two that may be sent as null — that is
    somebody ceasing to be a practitioner. `display_name` sent as null is the third case and
    not a deletion: blank means "call them by their name", which the handler recomputes.
    """

    first_name: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    last_name: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    display_name: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    is_practitioner: bool | None = None
    designation: Annotated[str | None, Field(max_length=64)] = None
    licence_number: Annotated[str | None, Field(max_length=64)] = None
    commission_rate_services_bp: Rate | None = None
    commission_rate_retail_bp: Rate | None = None
    colour: str | None = None
    max_concurrent_appointments: Annotated[int | None, Field(ge=1)] = None
    sort_order: int | None = None

    @field_validator(
        "designation", "licence_number", "display_name", "first_name", "last_name", mode="after"
    )
    @classmethod
    def _trimmed(cls, value: str | None) -> str | None:
        # A string of spaces passes `min_length` and is not a value. It becomes null, which
        # the handler then refuses for a required field and recomputes for `display_name`.
        return blank_to_none(value)

    @field_validator("colour")
    @classmethod
    def _known_colour(cls, value: str | None) -> str | None:
        return known_colour(value)


_CREDENTIALS = (
    "A practitioner needs a designation and a licence number — insurers reject treatment "
    "receipts without them."
)
# The three fields the rule above is about, so the check can be run against a payload before
# anything is written and against the patched row afterwards, with one implementation.
_CREDENTIAL_FIELDS = ("is_practitioner", "designation", "licence_number")


def _assert_credentials(staff: Staff) -> None:
    if staff.is_practitioner and not (staff.designation and staff.licence_number):
        raise refuse("licence_number" if staff.designation else "designation", _CREDENTIALS)


# --- reading ------------------------------------------------------------------------------


async def _roster(db: SessionDep, *, include_inactive: bool) -> list[StaffOut]:
    rows = (
        await db.execute(
            select(Staff, User)
            .join(User, User.id == Staff.user_id)
            .where(*([] if include_inactive else [Staff.active]))
            .order_by(Staff.active.desc(), Staff.sort_order, Staff.display_name)
        )
    ).all()
    locked = await locks_for([user.email for _, user in rows])
    return [_out(staff, user, locked.get(user.email)) for staff, user in rows]


@router.get("")
async def list_staff(
    _: StaffManager, db: SessionDep, include_inactive: bool = False
) -> dict[str, list[StaffOut]]:
    return {"staff": await _roster(db, include_inactive=include_inactive)}


class RosterEntry(BaseModel):
    """A column on the schedule. The palette key *and* its two hexes, so a screen that holds
    no `users.manage` — most of them — can paint without asking `/palette`."""

    id: str
    display_name: str
    colour: str
    hex: str
    dark_hex: str
    sort_order: int
    # The account behind the column — how a screen tells *its own* column from the others,
    # which is the line between overriding one's own evening and somebody else's (§22).
    user_id: str
    # Whether this person delivers services, as opposed to front-desk or admin-only staff.
    # The calendar's "Practitioners" picker reads this to decide who it offers, and a
    # practitioner's own default view (their own column only).
    is_practitioner: bool


@public.get("")
async def roster(_: Scheduler, db: SessionDep) -> dict[str, list[RosterEntry]]:
    """Active staff only, in column order. Anybody who may see the calendar may see who is
    on it; the account facts stay behind `users.manage` above."""
    rows = await db.scalars(
        select(Staff).where(Staff.active).order_by(Staff.sort_order, Staff.display_name)
    )
    return {
        "staff": [
            RosterEntry(
                id=str(s.id),
                display_name=s.display_name,
                colour=s.colour,
                hex=BY_KEY[s.colour].hex,
                dark_hex=BY_KEY[s.colour].dark_hex,
                sort_order=s.sort_order,
                user_id=str(s.user_id),
                is_practitioner=s.is_practitioner,
            )
            for s in rows
        ]
    }


@router.get("/palette")
async def list_palette(_: StaffManager) -> dict[str, list[ColourOut]]:
    """The picker's options. Served rather than duplicated in TypeScript for the same reason
    the brand palette is: the colour the calendar paints with is decided here."""
    return {
        "colours": [
            ColourOut(
                key=c.key,
                name=c.name,
                hex=c.hex,
                dark_hex=c.dark_hex,
                foreground=c.foreground,
                dark_foreground=c.dark_foreground,
            )
            for c in PALETTE
        ]
    }


# --- creating -----------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_staff(payload: StaffCreate, admin: StaffManager, db: SessionDep) -> StaffOut:
    # Before anything is written: a 422 must not be the tail end of a half-built account.
    _assert_credentials(Staff(**payload.model_dump(include=set(_CREDENTIAL_FIELDS))))

    role = await db.scalar(select(Role).where(Role.id == payload.role_id))
    if role is None:
        raise HTTPException(status_code=404, detail="No such role.")

    # No password, and none is invented. `password_hash` stays NULL until the invitation is
    # spent, `auth/login.py` refuses the account until then, and nothing ever emails a
    # credential that would work.
    user = User(email=payload.email.lower(), password_hash=None, role_id=role.id, role=role)
    db.add(user)
    try:
        # Flushed on its own, because the staff row needs the id the database generates —
        # and because a duplicate address has to become a 409 here rather than a 500 later.
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="An account with that email address already exists."
        ) from None

    staff = Staff(
        user_id=user.id,
        first_name=payload.first_name.strip(),
        last_name=payload.last_name.strip(),
        display_name=payload.display_name or f"{payload.first_name} {payload.last_name}".strip(),
        is_practitioner=payload.is_practitioner,
        designation=payload.designation,
        licence_number=payload.licence_number,
        commission_rate_services_bp=payload.commission_rate_services_bp,
        commission_rate_retail_bp=payload.commission_rate_retail_bp,
        colour=payload.colour or await _free_colour(db),
        max_concurrent_appointments=payload.max_concurrent_appointments,
        sort_order=payload.sort_order,
        active=True,
    )
    db.add(staff)
    await db.flush()

    record_event(
        db,
        "staff.created",
        target_type="staff",
        target_id=str(staff.id),
        actor_user_id=admin.id,
        metadata={
            "email": user.email,
            "display_name": staff.display_name,
            "role": role.name,
            "is_practitioner": staff.is_practitioner,
        },
    )
    token = await _issue_invitation(db, user, admin, "user.invited")
    await db.commit()
    _send_invitation(user.email, token, await _business_name(db))
    log.info("staff: %s invited %s", admin.email, user.email)
    return _out(staff, user, None)


async def _free_colour(db: SessionDep) -> str:
    """The first colour no active staff member is using, wrapping when all are taken."""
    taken = set(await db.scalars(select(Staff.colour).where(Staff.active)))
    active = await db.scalar(select(func.count()).select_from(Staff).where(Staff.active))
    return next_free(taken, active or 0)


# --- editing ------------------------------------------------------------------------------


@router.patch("/{staff_id}")
async def update_staff(
    staff_id: uuid.UUID, payload: StaffPatch, admin: StaffManager, db: SessionDep
) -> StaffOut:
    staff, user = await _load(db, staff_id)
    sent = payload.model_dump(exclude_unset=True)

    # Before anything is set — see `_admin_forms.refuse_emptied_field`.
    refuse_emptied_field(sent, _NOT_NULLABLE)

    changed = []
    for field, value in sent.items():
        # Applied after the names below, because clearing it means "recompute from them".
        if field == "display_name":
            continue
        if getattr(staff, field) != value:
            setattr(staff, field, value)
            changed.append(field)

    if "display_name" in sent:
        # Blank means "call them by their name", which is what the form's hint promises. The
        # fallback is read off the row, so a request that renames and clears in one go gets
        # the new name rather than the old one.
        wanted = sent["display_name"] or f"{staff.first_name} {staff.last_name}".strip()
        if staff.display_name != wanted:
            staff.display_name = wanted
            changed.append("display_name")

    _assert_credentials(staff)

    if changed:
        # Field names, not values. Which fields were touched is what an audit needs; copying
        # a licence number in would put it in a second table with a different retention
        # horizon, for no question it helps answer.
        record_event(
            db,
            "staff.updated",
            target_type="staff",
            target_id=str(staff.id),
            actor_user_id=admin.id,
            metadata={"email": user.email, "changed": sorted(changed)},
        )
    await db.commit()
    await cache.bump()
    return _out(staff, user, (await locks_for([user.email])).get(user.email))


# --- leaving, and coming back ---------------------------------------------------------------


async def _future_appointments(db: SessionDep, staff_id: uuid.UUID) -> int:
    """Confirmed appointments this person still has ahead of them. Deactivation neither
    cancels nor moves them — `/api/schedule` keeps drawing a column so the desk can — so the
    confirm dialog says how many are out there before anybody clicks (fix wave, finding 9)."""
    return await db.scalar(
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.staff_id == staff_id,
            Appointment.status == "confirmed",
            Appointment.starts_at >= datetime.now(UTC),
        )
    )


@router.post("/{staff_id}/deactivate")
async def deactivate_staff(staff_id: uuid.UUID, admin: StaffManager, db: SessionDep) -> StaffOut:
    """They stop being able to sign in. Everything they did stays exactly where it is.

    `revoke_all` rather than a denylist entry: no list of a person's live tokens exists, and
    somebody being walked out of the building must not keep a twelve-hour Staff Mode session
    on a phone in their pocket.
    """
    staff, user = await _load(db, staff_id)
    booked = await _future_appointments(db, staff.id)
    if not staff.active:
        return _out(staff, user, None, future_appointments=booked)

    staff.active = False
    revoke_all(user)
    # Asked of the state this change would produce, like every other caller. An instance
    # whose last administrator has just been deactivated has nobody who can undo it.
    await assert_an_administrator_remains(db)
    record_event(
        db,
        "staff.deactivated",
        target_type="staff",
        target_id=str(staff.id),
        actor_user_id=admin.id,
        metadata={"email": user.email, "display_name": staff.display_name},
    )
    await db.commit()
    await cache.bump()
    log.info("staff: %s deactivated %s", admin.email, user.email)
    return _out(staff, user, None, future_appointments=booked)


@router.post("/{staff_id}/reactivate")
async def reactivate_staff(staff_id: uuid.UUID, admin: StaffManager, db: SessionDep) -> StaffOut:
    staff, user = await _load(db, staff_id)
    if staff.active:
        return _out(staff, user, (await locks_for([user.email])).get(user.email))

    staff.active = True
    record_event(
        db,
        "staff.reactivated",
        target_type="staff",
        target_id=str(staff.id),
        actor_user_id=admin.id,
        metadata={"email": user.email, "display_name": staff.display_name},
    )
    await db.commit()
    await cache.bump()
    log.info("staff: %s reactivated %s", admin.email, user.email)
    return _out(staff, user, (await locks_for([user.email])).get(user.email))


# --- the invitation -------------------------------------------------------------------------


@router.post("/{staff_id}/resend-invite", status_code=202)
async def resend_invite(staff_id: uuid.UUID, admin: StaffManager, db: SessionDep) -> dict[str, str]:
    """A fresh link, and every earlier one dies with it.

    Refused once the account has a password: that request is a password reset, which is
    theirs to ask for from the sign-in screen. An administrator able to mint a live link into
    an account already in use is a different power from the one this endpoint grants.
    """
    staff, user = await _load(db, staff_id)
    if user.password_hash is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{staff.display_name} has already set a password. They can ask for a reset "
                "link from the sign-in screen."
            ),
        )

    token = await _issue_invitation(db, user, admin, "user.invite_resent")
    await db.commit()
    _send_invitation(user.email, token, await _business_name(db))
    log.info("staff: %s re-sent the invitation to %s", admin.email, user.email)
    return {"status": "sent"}


async def _issue_invitation(db: SessionDep, user: User, admin: User, event: str) -> str:
    """A reset token with a longer life, and the death of every earlier one.

    The same table and the same confirm endpoint as a forgotten password: an invitation *is*
    "set a password for this account, having proved you hold this mailbox", and a second
    mechanism for it would be a second place for single-use to be got wrong.
    """
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    await db.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
        .values(used_at=now)
    )
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=digest(token),
            expires_at=now + timedelta(hours=get_settings().invitation_hours),
        )
    )
    record_event(
        db,
        event,
        target_type="user",
        target_id=str(user.id),
        actor_user_id=admin.id,
        metadata={"email": user.email},
    )
    return token


def _send_invitation(email: str, token: str, business: str) -> None:
    """Enqueued after the caller's commit, deliberately: a worker fast enough to beat the
    transaction would send a link the confirm endpoint cannot find yet."""
    settings = get_settings()
    send_email.delay(
        email,
        f"You have been added to {business}",
        f"An administrator has created an account for you at {business}.\n\n"
        f"{settings.app_base_url}/reset-password?token={token}\n\n"
        f"Open the link to choose a password. It works once and expires in "
        f"{settings.invitation_hours} hours — ask for another if it does.",
    )


async def _business_name(db: SessionDep) -> str:
    return await db.scalar(select(Business.name).where(Business.id == 1)) or "LinSuite"


async def load_staff(db: SessionDep, staff_id: uuid.UUID) -> Staff:
    """The staff row, or the 404 every caller would otherwise write for itself.

    Public and here rather than in each of them, because this module owns the table:
    `scheduling/hours.py` and `scheduling/time_off.py` both hang off a staff member and must
    refuse an unknown one identically. `_load` below stays private — it also fetches the
    account, which only this screen needs.
    """
    staff = await db.get(Staff, staff_id)
    if staff is None:
        raise HTTPException(status_code=404, detail="No such staff member.")
    return staff


async def _load(db: SessionDep, staff_id: uuid.UUID) -> tuple[Staff, User]:
    row = (
        await db.execute(
            select(Staff, User).join(User, User.id == Staff.user_id).where(Staff.id == staff_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No such staff member.")
    return row[0], row[1]
