"""PHI reads are logged before opening sealed content; mutations return metadata only."""

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select

from auth.capabilities import Requires
from auth.models import User
from core import crypto
from core.access_log import LogAccess
from core.audit import record_event
from core.db import SessionDep
from core.errors import NOT_NOTE_AUTHOR, DocumentIntegrityError, Forbidden
from customers import keys, retention
from customers.models import Customer
from notes.models import NoteTemplate, SessionNote
from notes.schema import (
    AppointmentChoice,
    ContentIn,
    CreateIn,
    LockIn,
    NoteOut,
    NoteSummary,
    TemplateIn,
    TemplateOut,
    UpdateIn,
)
from scheduling.models import Appointment, Service, Staff

router = APIRouter(tags=["session notes"])
Writer = Annotated[User, Depends(Requires("notes.write"))]
Reader = Annotated[User, Depends(Requires("notes.view"))]
Manager = Annotated[User, Depends(Requires("notes.manage"))]


@router.get("/note-templates")
async def templates(_: Reader, db: SessionDep) -> list[TemplateOut]:
    rows = await db.scalars(select(NoteTemplate).order_by(NoteTemplate.name))
    return [
        TemplateOut(
            id=t.id, name=t.name, fields=t.fields, diagram_ids=t.diagram_ids, active=t.active
        )
        for t in rows
    ]


@router.get("/admin/note-templates")
async def admin_templates(user: Manager, db: SessionDep) -> list[TemplateOut]:
    return await templates(user, db)


@router.put("/note-templates/{template_id}")
async def update_template(
    template_id: uuid.UUID, body: TemplateIn, user: Manager, db: SessionDep
) -> TemplateOut:
    row = await db.get(NoteTemplate, template_id, with_for_update=True)
    if row is None:
        raise HTTPException(404, "No such note template.")
    for name, value in body.model_dump().items():
        setattr(row, name, value)
    record_event(
        db,
        "note_template.updated",
        target_type="note_template",
        target_id=str(row.id),
        actor_user_id=user.id,
    )
    await db.commit()
    return TemplateOut(id=row.id, **body.model_dump())


@router.post("/note-templates", status_code=201)
async def create_template(body: TemplateIn, user: Manager, db: SessionDep) -> TemplateOut:
    row = NoteTemplate(**body.model_dump())
    db.add(row)
    await db.flush()
    record_event(
        db,
        "note_template.created",
        target_type="note_template",
        target_id=str(row.id),
        actor_user_id=user.id,
    )
    await db.commit()
    return TemplateOut(id=row.id, **body.model_dump())


async def entry(db: SessionDep, customer_id: uuid.UUID, now: datetime) -> JSONResponse | None:
    """Stages the chart entry, or hands back the suppressed-client response every caller
    must return as-is. Matches the forms module's own customer_suppressed shape (#44/#47) —
    a 422 with a matchable `code`, not a bare 409."""
    try:
        await retention.record_clinical_entry(db, customer_id, now)
    except LookupError:
        raise HTTPException(404, "No such client.") from None
    except retention.CustomerSuppressed:
        return JSONResponse(
            {"code": "customer_suppressed", "detail": "This client's record is suppressed."},
            status_code=422,
        )
    return None


def associated(note: SessionNote) -> bytes:
    return b"session-note-v1:" + note.customer_id.bytes + note.id.bytes


def seal_content(body: ContentIn, note: SessionNote, key: bytes) -> bytes:
    return crypto.seal(
        body.model_dump_json(include={"answers", "annotations"}).encode(), key, associated(note)
    )


def stamp_annotations(body: ContentIn, existing: dict[uuid.UUID, datetime], now: datetime) -> None:
    """The server owns annotation timestamps, never the client: an existing annotation
    keeps the stamp it was first saved with, and a new one is stamped with `now`.
    """
    for annotation in body.annotations:
        annotation.timestamp = existing.get(annotation.id, now)


async def previous_annotation_timestamps(
    note: SessionNote, key: bytes
) -> dict[uuid.UUID, datetime]:
    try:
        previous = json.loads(crypto.open_sealed(note.content_sealed, key, associated(note)))
    except (InvalidTag, ValueError):
        raise DocumentIntegrityError(str(note.id)) from None
    return {
        uuid.UUID(a["id"]): datetime.fromisoformat(a["timestamp"]) for a in previous["annotations"]
    }


def check_content(body, snapshot: dict) -> None:
    if set(body.answers) - {f["key"] for f in snapshot["fields"]}:
        raise HTTPException(422, "An answer does not belong to this note template.")
    if any(a.diagram_id not in snapshot["diagram_ids"] for a in body.annotations):
        raise HTTPException(422, "An annotation does not belong to this note's diagrams.")


async def summary(note: SessionNote, user: User, db: SessionDep) -> NoteSummary:
    staff = await db.get(Staff, note.author_staff_id)
    return NoteSummary(
        id=note.id,
        template_id=note.template_id,
        appointment_id=note.appointment_id,
        author_staff_id=note.author_staff_id,
        author_name=staff.display_name,
        template=note.template_snapshot,
        revision=note.revision,
        created_at=note.created_at,
        updated_at=note.updated_at,
        locked_at=note.locked_at,
        can_edit=note.locked_at is None
        and staff.user_id == user.id
        and "notes.write" in user.capabilities,
    )


@router.get("/customers/{customer_id}/session-notes")
async def history(customer_id: uuid.UUID, user: Reader, db: SessionDep) -> list[NoteSummary]:
    if await db.get(Customer, customer_id) is None:
        raise HTTPException(404, "No such client.")
    rows = await db.scalars(
        select(SessionNote)
        .where(SessionNote.customer_id == customer_id)
        .order_by(SessionNote.created_at.desc(), SessionNote.id)
    )
    return [await summary(n, user, db) for n in rows]


@router.get("/customers/{customer_id}/session-note-appointments")
async def appointment_choices(
    customer_id: uuid.UUID, user: Writer, db: SessionDep
) -> list[AppointmentChoice]:
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(404, "No such client.")
    if customer.suppressed_at:
        return []
    rows = await db.execute(
        select(Appointment.id, Appointment.starts_at, Service.name)
        .join(Staff, Appointment.staff_id == Staff.id)
        .join(Service, Appointment.service_id == Service.id)
        .where(
            Appointment.customer_id == customer_id,
            Staff.user_id == user.id,
            Appointment.status.in_(("confirmed", "completed")),
        )
        .order_by(Appointment.starts_at.desc())
    )
    return [AppointmentChoice(id=r.id, starts_at=r.starts_at, service_name=r.name) for r in rows]


@router.post("/customers/{customer_id}/session-notes", status_code=201)
async def create_note(
    customer_id: uuid.UUID, body: CreateIn, user: Writer, db: SessionDep
) -> NoteSummary:
    now = datetime.now(UTC)
    # Customer lock serializes with erasure, and precedes appointment/note locks on every write.
    if (suppressed := await entry(db, customer_id, now)) is not None:
        return suppressed
    appointment = await db.scalar(
        select(Appointment)
        .where(Appointment.id == body.appointment_id)
        .with_for_update(of=Appointment)
    )
    if appointment is None or appointment.customer_id != customer_id:
        raise HTTPException(404, "No such appointment for this client.")
    staff = await db.get(Staff, appointment.staff_id)
    if staff.user_id != user.id:
        raise Forbidden(
            NOT_NOTE_AUTHOR, "Only the appointment's practitioner can author this note."
        )
    if appointment.status not in ("confirmed", "completed"):
        raise HTTPException(409, "A cancelled or missed appointment cannot start a session note.")
    template = await db.get(NoteTemplate, body.template_id, with_for_update=True)
    if template is None or not template.active:
        raise HTTPException(404, "No such active note template.")
    snapshot = TemplateIn(
        name=template.name, fields=template.fields, diagram_ids=template.diagram_ids, active=True
    ).model_dump()
    if body.template.model_dump() != snapshot:
        raise HTTPException(
            409,
            "This template changed while you were writing. "
            "Start a new note with the current wording.",
        )
    check_content(body, snapshot)
    stamp_annotations(body, existing={}, now=now)
    note = SessionNote(
        id=uuid.uuid4(),
        customer_id=customer_id,
        appointment_id=appointment.id,
        author_staff_id=staff.id,
        template_id=template.id,
        template_snapshot=snapshot,
        revision=1,
        created_at=now,
        updated_at=now,
    )
    note.content_sealed = seal_content(body, note, await keys.data_key(db, customer_id))
    db.add(note)
    record_event(
        db,
        "session_note.created",
        target_type="session_note",
        target_id=str(note.id),
        actor_user_id=user.id,
        metadata={"revision": 1},
    )
    await db.commit()
    return await summary(note, user, db)


@router.get(
    "/customers/{customer_id}/session-notes/{note_id}",
    dependencies=[Depends(LogAccess("session_note", "note_id"))],
)
async def get_note(
    customer_id: uuid.UUID, note_id: uuid.UUID, user: Reader, db: SessionDep, response: Response
) -> NoteOut:
    note = await db.get(SessionNote, note_id)
    if note is None or note.customer_id != customer_id:
        raise HTTPException(404, "No such session note.")
    key = await keys.existing_key(db, customer_id)
    if key is None:
        raise HTTPException(404, "No such session note.")
    try:
        content = json.loads(crypto.open_sealed(note.content_sealed, key, associated(note)))
    except (InvalidTag, ValueError):
        raise DocumentIntegrityError(str(note.id)) from None
    response.headers["Cache-Control"] = "no-store"
    return NoteOut(**(await summary(note, user, db)).model_dump(), **content)


async def editable(
    db: SessionDep, customer_id: uuid.UUID, note_id: uuid.UUID, user: User, revision: int
) -> SessionNote:
    note = await db.get(SessionNote, note_id, with_for_update=True)
    if note is None or note.customer_id != customer_id:
        raise HTTPException(404, "No such session note.")
    author = await db.get(Staff, note.author_staff_id)
    if author.user_id != user.id:
        raise Forbidden(NOT_NOTE_AUTHOR, "Only this note's author can change it.")
    if note.locked_at is not None:
        raise HTTPException(409, "This note is locked and cannot be changed.")
    if note.revision != revision:
        raise HTTPException(409, "This note changed in another window. Reopen it before saving.")
    return note


@router.put("/customers/{customer_id}/session-notes/{note_id}")
async def update_note(
    customer_id: uuid.UUID, note_id: uuid.UUID, body: UpdateIn, user: Writer, db: SessionDep
) -> NoteSummary:
    now = datetime.now(UTC)
    if (suppressed := await entry(db, customer_id, now)) is not None:
        return suppressed
    note = await editable(db, customer_id, note_id, user, body.revision)
    check_content(body, note.template_snapshot)
    key = await keys.existing_key(db, customer_id)
    if key is None:
        raise HTTPException(404, "No such session note.")
    stamp_annotations(body, existing=await previous_annotation_timestamps(note, key), now=now)
    note.content_sealed = seal_content(body, note, key)
    note.revision += 1
    note.updated_at = now
    record_event(
        db,
        "session_note.updated",
        target_type="session_note",
        target_id=str(note.id),
        actor_user_id=user.id,
        metadata={"revision": note.revision},
    )
    await db.commit()
    return await summary(note, user, db)


@router.post("/customers/{customer_id}/session-notes/{note_id}/lock")
async def lock_note(
    customer_id: uuid.UUID, note_id: uuid.UUID, body: LockIn, user: Writer, db: SessionDep
) -> NoteSummary:
    now = datetime.now(UTC)
    if (suppressed := await entry(db, customer_id, now)) is not None:
        return suppressed
    note = await editable(db, customer_id, note_id, user, body.revision)
    key = await keys.existing_key(db, customer_id)
    if key is None:
        raise HTTPException(404, "No such session note.")
    try:
        content = ContentIn.model_validate_json(
            crypto.open_sealed(note.content_sealed, key, associated(note))
        )
    except (InvalidTag, ValueError):
        raise DocumentIntegrityError(str(note.id)) from None
    for field in note.template_snapshot["fields"]:
        if field["required"] and not content.answers.get(field["key"], "").strip():
            raise HTTPException(422, f"Complete {field['label']} before locking this note.")
    if not any(v.strip() for v in content.answers.values()) and not content.annotations:
        raise HTTPException(422, "An empty note cannot be locked.")
    note.locked_at = now
    note.locked_by_user_id = user.id
    note.updated_at = now
    note.revision += 1
    record_event(
        db,
        "session_note.locked",
        target_type="session_note",
        target_id=str(note.id),
        actor_user_id=user.id,
        metadata={"revision": note.revision},
    )
    await db.commit()
    return await summary(note, user, db)
