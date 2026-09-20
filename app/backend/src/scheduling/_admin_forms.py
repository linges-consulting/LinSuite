"""Shared shapes for the admin CRUD screens under `scheduling/` — `staff.py` and
`resources.py`, and any later one cut from the same cloth.

Every one of these screens' PATCH models has the same problem: every field is `… | None` so
"leave it alone" and "set it" can be told apart, which means a null reaches a NOT NULL column
unless something stops it, and a blank string passes `min_length=1` without being a value.
Two independent answers to the same question is the failure mode this module exists to
close — a future change to the 422 shape or the blank-string rule would otherwise have to be
remembered in two files instead of made in one.

Lives beside `staff.py`/`resources.py` rather than in `core/`: `known_colour` reads
`scheduling/palette.py`, and `core/` is imported by every domain, never the other way round —
putting this here keeps that direction intact.
"""

from fastapi import HTTPException

from scheduling.palette import BY_KEY


def blank_to_none(value: str | None) -> str | None:
    """A cleared text input sends `""`, and an empty value is not a short one."""
    return value.strip() or None if isinstance(value, str) else value


def refuse(field: str, message: str, *, where: str = "body") -> HTTPException:
    """Shaped like FastAPI's own 422, so the frontend reads one error format. `where` is
    `body` for a form field and `query` for a query parameter, as FastAPI would say it."""
    return HTTPException(
        status_code=422, detail=[{"type": "value_error", "loc": [where, field], "msg": message}]
    )


def refuse_emptied_field(sent: dict, not_nullable: tuple[str, ...]) -> None:
    """Raise `refuse` for the first NOT NULL column a PATCH tried to null out.

    `sent` is `payload.model_dump(exclude_unset=True)`: a field absent from it means "leave
    it alone" and is never a candidate here, only one explicitly sent as `null` is. Blind-
    copying that null onto the row reaches the database as a NOT NULL violation at commit,
    which the caller would read as a 500 — an answer that says the server broke rather than
    that the request asked for something it cannot have.
    """
    emptied = [field for field in not_nullable if sent.get(field, ...) is None]
    if emptied:
        raise refuse(
            emptied[0], "This cannot be emptied. Send a value, or leave the field out to keep it."
        )


def known_colour(value: str | None) -> str | None:
    """A Pydantic validator body: `None` (no colour, or "leave it alone") passes through,
    anything else has to be a real key from the curated palette."""
    if value is not None and value not in BY_KEY:
        raise ValueError("not one of the staff colours")
    return value
