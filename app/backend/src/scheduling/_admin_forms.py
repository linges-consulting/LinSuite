"""Shared shapes for the admin CRUD screens under `scheduling/` — `staff.py` and
`resources.py`, and any later one cut from the same cloth.

The domain-neutral helpers live in `core/forms.py` (re-exported here for scheduling's own
modules); only `known_colour` is scheduling's, because it reads `scheduling/palette.py`.
"""

from core.forms import blank_to_none, refuse, refuse_emptied_field
from scheduling.palette import BY_KEY

__all__ = ["blank_to_none", "known_colour", "refuse", "refuse_emptied_field"]


def known_colour(value: str | None) -> str | None:
    """A Pydantic validator body: `None` (no colour, or "leave it alone") passes through,
    anything else has to be a real key from the curated palette."""
    if value is not None and value not in BY_KEY:
        raise ValueError("not one of the staff colours")
    return value
