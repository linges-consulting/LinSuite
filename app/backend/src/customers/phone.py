"""S2: phone number normalisation for lookup (Phase 14, #16).

`customers.phone` is digits-only but not otherwise normalised (`customers/routes.py::
_digits` strips formatting and nothing else), so a number entered as `"+1 416 555 0199"` is
stored with its leading `1` (11 digits) and one entered as `"416-555-0199"` is not (10) — two
callers who are the same person on paper store differently depending on how the front desk
typed it in. `normalize_phone` is the one NANP `+1`/leading-`1` transform every phone-lookup
comparison in this codebase applies, on both sides: to whatever a caller ID or a staff member
typed, and — as the identical SQL expression, `normalized_phone_column` — to every stored
`customers.phone` value it is compared against. Migration 0071's functional index covers
exactly this expression, which is why a new denormalised column was never on the table: two
copies of a phone number can drift, one index cannot.
"""

import re

from sqlalchemy import and_, case, func
from sqlalchemy.sql.elements import ColumnElement

# Below this many digits a search matches too much of the customer list to be a caller-id
# lookup rather than a fishing expedition (decision #2 in the ticket).
MIN_PARTIAL_DIGITS = 4

# The functional index's expression: `customers/models.py` builds its `Index` from this
# constant, and migration 0071 froze a copy of it (migrations never import app code). Both
# have to compile to what `normalized_phone_column` emits, or the index stops covering it.
NORMALIZED_PHONE_SQL = (
    "CASE WHEN length(phone) = 11 AND left(phone, 1) = '1' THEN substr(phone, 2) ELSE phone END"
)


def normalize_phone(raw: str | None) -> str:
    """Digits only, then the leading NANP country code dropped when it is there: `"+1 (416)
    555-0199"`, `"1-416-555-0199"` and `"416.555.0199"` all normalise to `"4165550199"`."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] == "1":
        return digits[1:]
    return digits


def normalized_phone_column(column: ColumnElement[str]) -> ColumnElement[str]:
    """`normalize_phone`, as a SQL expression over a `phone` column — must stay the exact
    expression `NORMALIZED_PHONE_SQL` names, or Postgres will not match it to the functional index
    (still correct, just an unindexed scan)."""
    return case(
        (and_(func.length(column) == 11, func.left(column, 1) == "1"), func.substr(column, 2)),
        else_=column,
    )
