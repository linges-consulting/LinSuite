"""Settings → Forms: build a template, publish it as a numbered frozen version, retire it.

Everything here is `forms.manage`, which the registry marks administrative, so an Admin Mode
window is required on top of the capability (`Requires` applies it).

**Draft, then publish** (tech-stack §18). `PUT …/draft` replaces the one editable draft —
schema, name, kind and the two versioned flags — after `forms.schema.FormSchema` has
validated it. `POST …/publish` copies the draft into `form_template_versions` as the next
number; that row is never rewritten (REVOKE + trigger, 0027). Publishing a draft identical to
the latest version is 409 `draft_unchanged`: two numbers for one form would make "which
version did she sign" a question with two right answers. The name and kind are frozen on the
version with the fields: a rename is a new version, and `template.name` is only the list label.

**Keys are the builder's**, minted with `crypto.randomUUID()` when a field is added and kept
through every rewording. The server checks their shape and uniqueness (`FormSchema`) and one
thing the schema alone cannot: a key already published keeps its type, because the answers
filed under it in every version must mean the same kind of thing.

**Retire, never delete, once published.** A published version may already sit behind a link
or a submission. A template that was never published has nothing citing it and is deleted.

Each template row is locked `FOR UPDATE` by every write, so two publishes cannot both read
`max(number)` and two tabs cannot delete a template while the other publishes it.

Templates hold no client data, so nothing here writes the access log (ADR-0002); every
mutation writes an audit event with identifiers and numbers only.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select

from auth.capabilities import Requires
from auth.models import User
from core.audit import record_event
from core.db import SessionDep
from forms.models import FormTemplate, FormTemplateVersion
from forms.schema import FormSchema, kind_problems
from scheduling._admin_forms import refuse

router = APIRouter(prefix="/admin/forms", tags=["forms"])

FormManager = Annotated[User, Depends(Requires("forms.manage"))]
Kind = Literal["intake", "consent", "waiver", "other"]


# --- what goes over the wire -----------------------------------------------------------------


class Draft(BaseModel):
    schema_: dict[str, Any] = Field(alias="schema", serialization_alias="schema")
    is_health_form: bool
    is_mandatory: bool


class TemplateOut(BaseModel):
    id: str
    name: str
    kind: Kind
    retired_at: datetime | None
    updated_at: datetime
    latest_version: int | None
    # The draft differs from the latest version (or nothing is published yet and the draft has
    # fields): there is something to publish.
    has_unpublished_changes: bool
    draft: Draft


class VersionSummary(BaseModel):
    number: int
    # As published. Read these, never the template's, wherever a client or a record is shown.
    name: str
    kind: Kind
    published_at: datetime
    requires_resignature: bool
    is_health_form: bool
    is_mandatory: bool


class VersionOut(VersionSummary):
    id: str
    template_id: str
    schema_: dict[str, Any] = Field(serialization_alias="schema")


Name = Annotated[str, Field(min_length=1, max_length=200)]


class _Named(BaseModel):
    name: Name
    kind: Kind

    @field_validator("name", mode="after")
    @classmethod
    def _real_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("this cannot be blank")
        return value.strip()


class TemplateIn(_Named):
    is_health_form: bool = False
    is_mandatory: bool = False


class DraftIn(_Named):
    schema_: FormSchema = Field(alias="schema")
    is_health_form: bool = False
    is_mandatory: bool = False

    @model_validator(mode="after")
    def _kind_rules(self) -> "DraftIn":
        for problem in kind_problems(self.schema_, self.kind):
            raise ValueError(problem)
        return self


class PublishIn(BaseModel):
    requires_resignature: bool = False


def _coded(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "code": code})


def _flags(row: FormTemplate | FormTemplateVersion) -> tuple:
    """Everything a version freezes. Any difference is something to publish."""
    if isinstance(row, FormTemplate):
        return (
            row.name,
            row.kind,
            row.draft_schema,
            row.draft_is_health_form,
            row.draft_is_mandatory,
        )
    return (row.name, row.kind, row.schema, row.is_health_form, row.is_mandatory)


def _out(template: FormTemplate, latest: FormTemplateVersion | None) -> TemplateOut:
    if latest is None:
        changed = bool(template.draft_schema["fields"])
    else:
        changed = _flags(template) != _flags(latest)
    return TemplateOut(
        id=str(template.id),
        name=template.name,
        kind=template.kind,
        retired_at=template.retired_at,
        updated_at=template.updated_at,
        latest_version=latest.number if latest else None,
        has_unpublished_changes=changed,
        draft=Draft(
            schema=template.draft_schema,
            is_health_form=template.draft_is_health_form,
            is_mandatory=template.draft_is_mandatory,
        ),
    )


def _version_out(version: FormTemplateVersion) -> VersionOut:
    return VersionOut(
        id=str(version.id),
        template_id=str(version.template_id),
        number=version.number,
        name=version.name,
        kind=version.kind,
        published_at=version.published_at,
        requires_resignature=version.requires_resignature,
        is_health_form=version.is_health_form,
        is_mandatory=version.is_mandatory,
        schema_=version.schema,
    )


# --- reading ---------------------------------------------------------------------------------


async def _template(db: SessionDep, template_id: uuid.UUID, *, lock: bool = False) -> FormTemplate:
    query = select(FormTemplate).where(FormTemplate.id == template_id)
    template = await db.scalar(query.with_for_update() if lock else query)
    if template is None:
        raise HTTPException(status_code=404, detail="No such form.")
    return template


async def _latest(db: SessionDep, template_id: uuid.UUID) -> FormTemplateVersion | None:
    return await db.scalar(
        select(FormTemplateVersion)
        .where(FormTemplateVersion.template_id == template_id)
        .order_by(FormTemplateVersion.number.desc())
        .limit(1)
    )


@router.get("")
async def list_templates(_: FormManager, db: SessionDep) -> dict[str, list[TemplateOut]]:
    """Active before retired, then by name. One query for the latest version of each."""
    templates = await db.scalars(
        select(FormTemplate).order_by(
            FormTemplate.retired_at.is_not(None), func.lower(FormTemplate.name)
        )
    )
    latest = {
        v.template_id: v
        for v in await db.scalars(
            select(FormTemplateVersion)
            .distinct(FormTemplateVersion.template_id)
            .order_by(FormTemplateVersion.template_id, FormTemplateVersion.number.desc())
        )
    }
    return {"templates": [_out(t, latest.get(t.id)) for t in templates]}


@router.get("/{template_id}")
async def get_template(template_id: uuid.UUID, _: FormManager, db: SessionDep) -> TemplateOut:
    return _out(await _template(db, template_id), await _latest(db, template_id))


@router.get("/{template_id}/versions")
async def list_versions(
    template_id: uuid.UUID, _: FormManager, db: SessionDep
) -> dict[str, list[VersionSummary]]:
    await _template(db, template_id)
    versions = await db.scalars(
        select(FormTemplateVersion)
        .where(FormTemplateVersion.template_id == template_id)
        .order_by(FormTemplateVersion.number.desc())
    )
    return {"versions": [VersionSummary.model_validate(v, from_attributes=True) for v in versions]}


@router.get("/{template_id}/versions/{number}")
async def get_version(
    template_id: uuid.UUID, number: int, _: FormManager, db: SessionDep
) -> VersionOut:
    version = await db.scalar(
        select(FormTemplateVersion).where(
            FormTemplateVersion.template_id == template_id, FormTemplateVersion.number == number
        )
    )
    if version is None:
        raise HTTPException(status_code=404, detail="No such version.")
    return _version_out(version)


# --- writing ---------------------------------------------------------------------------------


def _audit(db: SessionDep, event: str, template: FormTemplate, admin: User, **metadata) -> None:
    record_event(
        db,
        f"form_template.{event}",
        target_type="form_template",
        target_id=str(template.id),
        actor_user_id=admin.id,
        metadata=metadata,
    )


@router.post("", status_code=201)
async def create_template(payload: TemplateIn, admin: FormManager, db: SessionDep) -> TemplateOut:
    template = FormTemplate(
        name=payload.name,
        kind=payload.kind,
        draft_schema={"fields": []},
        draft_is_health_form=payload.is_health_form,
        draft_is_mandatory=payload.is_mandatory,
    )
    db.add(template)
    await db.flush()
    _audit(db, "created", template, admin)
    await db.commit()
    await db.refresh(template)
    return _out(template, None)


def _retired() -> JSONResponse:
    return _coded(409, "template_retired", "This form is retired. Its versions stay as they are.")


@router.put("/{template_id}/draft")
async def save_draft(
    template_id: uuid.UUID, payload: DraftIn, admin: FormManager, db: SessionDep
) -> TemplateOut:
    template = await _template(db, template_id, lock=True)
    if template.retired_at is not None:
        return _retired()
    # Every version, not just the latest: a key dropped in v2 and brought back in v3 still
    # has v1's answers filed under it.
    published = {
        f["key"]: f["type"]
        for schema in await db.scalars(
            select(FormTemplateVersion.schema)
            .where(FormTemplateVersion.template_id == template_id)
            .order_by(FormTemplateVersion.number)
        )
        for f in schema["fields"]
    }
    for field in payload.schema_.fields:
        if published.get(field.key, field.type) != field.type:
            raise refuse(
                "schema",
                f"“{field.label}” was published as another type. Remove it and add a new "
                "field instead.",
            )
    latest = await _latest(db, template_id)

    template.name = payload.name
    template.kind = payload.kind
    template.draft_schema = payload.schema_.model_dump(mode="json", exclude_none=True)
    template.draft_is_health_form = payload.is_health_form
    template.draft_is_mandatory = payload.is_mandatory
    template.updated_at = datetime.now(UTC)
    _audit(db, "draft_saved", template, admin)
    await db.commit()
    return _out(template, latest)


@router.post("/{template_id}/publish", status_code=201)
async def publish(
    template_id: uuid.UUID, payload: PublishIn, admin: FormManager, db: SessionDep
) -> VersionOut:
    template = await _template(db, template_id, lock=True)
    if template.retired_at is not None:
        return _retired()
    if not template.draft_schema["fields"]:
        return _coded(422, "draft_empty", "Add at least one field before publishing.")
    latest = await _latest(db, template_id)
    if latest is None and payload.requires_resignature:
        return _coded(
            422,
            "nothing_to_resign",
            "The first version has no earlier signatures to replace. Publish it without "
            "requiring re-signature.",
        )
    if latest is not None and _flags(latest) == _flags(template):
        return _coded(409, "draft_unchanged", f"Nothing has changed since version {latest.number}.")

    version = FormTemplateVersion(
        template_id=template.id,
        number=(latest.number if latest else 0) + 1,
        name=template.name,
        kind=template.kind,
        schema=template.draft_schema,
        is_health_form=template.draft_is_health_form,
        is_mandatory=template.draft_is_mandatory,
        requires_resignature=payload.requires_resignature,
        published_by_user_id=admin.id,
    )
    db.add(version)
    _audit(db, "published", template, admin, number=version.number)
    await db.commit()
    await db.refresh(version)
    return _version_out(version)


@router.post("/{template_id}/retire")
async def retire(template_id: uuid.UUID, admin: FormManager, db: SessionDep) -> TemplateOut:
    template = await _template(db, template_id, lock=True)
    if template.retired_at is None:
        template.retired_at = template.updated_at = datetime.now(UTC)
        _audit(db, "retired", template, admin)
        await db.commit()
    return _out(template, await _latest(db, template_id))


@router.delete("/{template_id}", status_code=204)
async def delete_template(template_id: uuid.UUID, admin: FormManager, db: SessionDep) -> Response:
    template = await _template(db, template_id, lock=True)
    if await _latest(db, template_id) is not None:
        return _coded(409, "template_published", "A published form is retired, never deleted.")
    _audit(db, "deleted", template, admin)
    await db.delete(template)
    await db.commit()
    return Response(status_code=204)
