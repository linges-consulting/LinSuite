import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

DiagramId = Literal["body_front", "body_back", "layout"]


class NoteField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    label: str = Field(min_length=1, max_length=200)
    required: bool = False


class TemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    fields: list[NoteField] = Field(min_length=1, max_length=30)
    diagram_ids: list[DiagramId] = Field(default_factory=list, max_length=3)
    active: bool = True

    @model_validator(mode="after")
    def valid_definition(self):
        if not self.name.strip() or any(not f.label.strip() for f in self.fields):
            raise ValueError("Names and field labels cannot be blank.")
        if len({f.key for f in self.fields}) != len(self.fields):
            raise ValueError("Field keys must be unique.")
        if len(set(self.diagram_ids)) != len(self.diagram_ids):
            raise ValueError("Diagrams must be unique.")
        return self


class TemplateOut(TemplateIn):
    id: uuid.UUID


class Annotation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: uuid.UUID
    kind: Literal["pin", "zone", "text"]
    diagram_id: DiagramId
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    colour: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    timestamp: AwareDatetime
    text: str = Field(default="", max_length=2000)
    width: float | None = Field(default=None, gt=0, le=1)
    height: float | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def shape(self):
        if self.kind == "zone":
            if self.width is None or self.height is None:
                raise ValueError("A shaded zone needs width and height.")
            if self.x + self.width > 1.0000001 or self.y + self.height > 1.0000001:
                raise ValueError("A shaded zone must fit inside the diagram.")
        elif self.width is not None or self.height is not None:
            raise ValueError("Only shaded zones have width and height.")
        if self.kind == "text" and not self.text.strip():
            raise ValueError("A text annotation cannot be blank.")
        return self


class ContentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: dict[str, Annotated[str, Field(max_length=20000)]] = Field(
        default_factory=dict, max_length=30
    )
    annotations: list[Annotation] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def unique_annotations(self):
        if len({a.id for a in self.annotations}) != len(self.annotations):
            raise ValueError("Annotation identifiers must be unique.")
        return self


class CreateIn(ContentIn):
    appointment_id: uuid.UUID
    template_id: uuid.UUID
    template: TemplateIn


class UpdateIn(ContentIn):
    revision: int = Field(ge=1)


class LockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)


class NoteSummary(BaseModel):
    id: uuid.UUID
    template_id: uuid.UUID
    appointment_id: uuid.UUID
    author_staff_id: uuid.UUID
    author_name: str
    template: TemplateIn
    revision: int
    created_at: datetime
    updated_at: datetime
    locked_at: datetime | None
    can_edit: bool


class NoteOut(NoteSummary):
    answers: dict[str, str]
    annotations: list[Annotation]


class AppointmentChoice(BaseModel):
    id: uuid.UUID
    starts_at: datetime
    service_name: str
