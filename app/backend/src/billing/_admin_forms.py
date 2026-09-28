"""Shared shapes for billing's admin CRUD screens — `packages.py`, and any later one cut from
the same cloth (discount definitions, tax components).

Every PATCH model here has every field `… | None` so "leave it alone" and "set it" can be
told apart, which means a null reaches a NOT NULL column unless something stops it, and a
blank string passes `min_length=1` without being a value. Copied from `scheduling
._admin_forms`, deliberately, rather than imported from it: that module lives beside
`staff.py`/`resources.py` because `known_colour` reads `scheduling.palette`, and `core/` is
imported by every domain, never the other way round — the same argument keeps this copy here
rather than in either place.
"""

from fastapi import HTTPException


def blank_to_none(value: str | None) -> str | None:
    """A cleared text input sends `""`, and an empty value is not a short one."""
    return value.strip() or None if isinstance(value, str) else value


def refuse(field: str, message: str, *, where: str = "body") -> HTTPException:
    """Shaped like FastAPI's own 422, so the frontend reads one error format."""
    return HTTPException(
        status_code=422, detail=[{"type": "value_error", "loc": [where, field], "msg": message}]
    )


def refuse_emptied_field(sent: dict, not_nullable: tuple[str, ...]) -> None:
    """Raise `refuse` for the first NOT NULL column a PATCH tried to null out.

    `sent` is `payload.model_dump(exclude_unset=True)`: a field absent from it means "leave
    it alone" and is never a candidate here, only one explicitly sent as `null` is.
    """
    emptied = [field for field in not_nullable if sent.get(field, ...) is None]
    if emptied:
        raise refuse(
            emptied[0], "This cannot be emptied. Send a value, or leave the field out to keep it."
        )
