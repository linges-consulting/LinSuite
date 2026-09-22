"""The form schema: what a template's fields are, and what counts as answering them.

**One format for every form** (tech-stack §18). A schema is `{"fields": [...]}`, in order;
each field has a `key` — a UUID minted by the builder when the field is added and kept for
as long as the field exists, whatever its label is reworded to, so "Do you have diabetes?"
becoming "Any diabetes diagnosis?" is still the same answer in every version. The key is the
identity; the label is prose.

**Conditional display is one level deep.** `show_if` names an *earlier* yes/no or choice field
that is not itself conditional, and the values that reveal this one. Earlier-only rules out
cycles by construction; one level means a hidden field can never be what reveals another.

**A hidden field is never required and its answer is dropped.** A client who answered "yes",
typed a note, then switched to "no" must not have that note filed in their chart.

**Twin:** `app/frontend/src/lib/forms.ts` applies the same rules for the builder's preview
and the fill page. Both suites read `app/shared/form-schema-cases.json`; change the two
together or that fixture fails one of them.

Answers (Task 4 files them; the rules live here so both sides agree):

- `yes_no`: `"yes"` or `"no"`. `single_choice`: one of `options`. `multi_choice`: a list of
  distinct `options`. `short_text`/`long_text`: a string. `date`: `YYYY-MM-DD`, a real day.
  `acknowledgement`: `true` (unticked is unanswered). `signature`: `{"name": typed full name,
  "image": the drawn signature as a data URL}` — owner ruling Q3: signed means both.
- `heading` and `paragraph` carry no answer.
"""

import re
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FieldType = Literal[
    "heading",
    "paragraph",
    "short_text",
    "long_text",
    "yes_no",
    "single_choice",
    "multi_choice",
    "date",
    "acknowledgement",
    "signature",
]
DISPLAY = frozenset({"heading", "paragraph"})
CHOICE = frozenset({"single_choice", "multi_choice"})
# What `show_if` may point at: a field whose answer is one of a known set of values.
CONDITION_SOURCES = CHOICE | {"yes_no"}
YES_NO = ("yes", "no")

MAX_FIELDS = 200
MAX_OPTIONS = 50
# A paragraph or an acknowledgement *is* its label — a consent clause can run to pages.
LONG_LABEL_TYPES = frozenset({"paragraph", "acknowledgement"})
MAX_LABEL, MAX_LONG_LABEL, MAX_HELP, MAX_OPTION = 500, 20_000, 2_000, 200
MAX_TEXT = {"short_text": 500, "long_text": 10_000}

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _uuid(value: str) -> str:
    # Canonical dashed form only, lower-cased — `uuid.UUID` would also take braces, a URN or
    # 32 bare hex digits, and the TypeScript twin would then disagree about what a key is.
    if not isinstance(value, str) or not UUID_RE.match(value):
        raise ValueError("a field key must be a UUID")
    return value.lower()


class ShowIf(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    equals: Annotated[list[str], Field(min_length=1, max_length=MAX_OPTIONS)]

    @field_validator("equals")
    @classmethod
    def _distinct(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("the same answer is listed twice")
        return value

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        return _uuid(value)


class FormField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    type: FieldType
    label: str
    help: Annotated[str, Field(max_length=MAX_HELP)] | None = None
    required: bool = False
    options: list[str] | None = None
    show_if: ShowIf | None = None

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        return _uuid(value)

    @model_validator(mode="after")
    def _own_rules(self) -> "FormField":
        self.label = self.label.strip()
        limit = MAX_LONG_LABEL if self.type in LONG_LABEL_TYPES else MAX_LABEL
        if not self.label:
            raise ValueError("every field needs a label")
        if len(self.label) > limit:
            raise ValueError(f"a label is at most {limit} characters")
        if self.help is not None:
            self.help = self.help.strip() or None
        if self.type in DISPLAY and self.required:
            raise ValueError("a heading or paragraph has nothing to answer, so cannot be required")
        if self.type == "signature" and not self.required:
            # Owner ruling Q3: a signature block on the template is what makes signing required.
            raise ValueError("a signature block is always required")
        if self.type in CHOICE:
            options = [o.strip() for o in self.options or []]
            if not options:
                raise ValueError("a choice field needs at least one option")
            if len(options) > MAX_OPTIONS:
                raise ValueError(f"at most {MAX_OPTIONS} options")
            if any(not o or len(o) > MAX_OPTION for o in options):
                raise ValueError(f"every option needs text, at most {MAX_OPTION} characters")
            if len(set(options)) != len(options):
                raise ValueError("the same option is listed twice")
            self.options = options
        elif self.options is not None:
            raise ValueError("only a choice field has options")
        return self


class FormSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: Annotated[list[FormField], Field(max_length=MAX_FIELDS)]

    @model_validator(mode="after")
    def _cross_field_rules(self) -> "FormSchema":
        earlier: dict[str, FormField] = {}
        signatures = 0
        for field in self.fields:
            if field.key in earlier:
                raise ValueError("two fields share one key")
            if field.show_if is not None:
                if field.type == "signature":
                    # Owner ruling Q3: a signature block present means signing is required. A
                    # conditional one would let a client answer their way out of signing.
                    raise ValueError("a signature block cannot be conditional")
                source = earlier.get(field.show_if.key)
                if source is None:
                    raise ValueError("“show only if” must refer to an earlier field")
                if source.type not in CONDITION_SOURCES:
                    raise ValueError("“show only if” must refer to a yes/no or choice field")
                if source.show_if is not None:
                    raise ValueError("“show only if” cannot refer to a field that is conditional")
                allowed = YES_NO if source.type == "yes_no" else source.options or []
                if any(value not in allowed for value in field.show_if.equals):
                    raise ValueError("“show only if” names a value that field cannot have")
            signatures += field.type == "signature"
            earlier[field.key] = field
        if signatures > 1:
            raise ValueError("a form has at most one signature block")
        return self


# Kinds whose statements are the point of the form: an acknowledgement on a consent or a waiver
# is a clause the client agrees to, and "show only if" would let an answer skip it.
BINDING_KINDS = frozenset({"consent", "waiver"})


def kind_problems(schema: FormSchema, kind: str) -> list[str]:
    """The rules that depend on the template's kind, which the schema itself does not carry."""
    if kind in BINDING_KINDS and any(
        f.type == "acknowledgement" and f.show_if is not None for f in schema.fields
    ):
        return [f"an acknowledgement on a {kind} cannot be conditional"]
    return []


# --- answers ---------------------------------------------------------------------------------


def _empty(value: Any) -> bool:
    return (
        value is None
        or value is False
        or (isinstance(value, str) and not value.strip())
        or (isinstance(value, list) and not value)
    )


def _is_shown(field: FormField, answers: dict[str, Any]) -> bool:
    if field.show_if is None:
        return True
    answer = answers.get(field.show_if.key)
    chosen = answer if isinstance(answer, list) else [answer]
    return any(value in field.show_if.equals for value in chosen)


def visible_keys(schema: FormSchema, answers: dict[str, Any]) -> list[str]:
    """The keys of the fields shown for these answers, in form order (display fields too)."""
    return [f.key for f in schema.fields if _is_shown(f, answers)]


def kept_answers(schema: FormSchema, answers: dict[str, Any]) -> dict[str, Any]:
    """The answers worth filing: visible, answerable, non-empty — in form order. Anything else
    (a hidden field's leftover, an unknown key, a heading) is dropped, never stored."""
    shown = set(visible_keys(schema, answers))
    return {
        f.key: answers[f.key]
        for f in schema.fields
        if f.key in shown and f.type not in DISPLAY and not _empty(answers.get(f.key))
    }


def _valid(field: FormField, value: Any) -> bool:
    match field.type:
        case "yes_no":
            return value in YES_NO
        case "single_choice":
            return isinstance(value, str) and value in (field.options or [])
        case "multi_choice":
            return (
                isinstance(value, list)
                and len(set(map(str, value))) == len(value)
                and all(isinstance(v, str) and v in (field.options or []) for v in value)
            )
        case "short_text" | "long_text":
            return isinstance(value, str) and len(value) <= MAX_TEXT[field.type]
        case "date":
            if not isinstance(value, str) or not DATE_RE.match(value):
                return False
            try:
                date.fromisoformat(value)
            except ValueError:
                return False
            return True
        case "acknowledgement":
            return value is True
        case "signature":
            return (
                isinstance(value, dict)
                and set(value) == {"name", "image"}
                and all(isinstance(value[k], str) and value[k].strip() for k in value)
            )
    return False


def validate_answers(schema: FormSchema, answers: dict[str, Any]) -> dict[str, str]:
    """`{key: "required" | "invalid"}` for each visible field that fails; empty means valid.
    Codes, not sentences: the fill page words them, and the twin must produce the same."""
    shown = set(visible_keys(schema, answers))
    errors: dict[str, str] = {}
    for field in schema.fields:
        if field.key not in shown or field.type in DISPLAY:
            continue
        value = answers.get(field.key)
        if _empty(value):
            if field.required:
                errors[field.key] = "required"
        elif not _valid(field, value):
            errors[field.key] = "invalid"
    return errors
