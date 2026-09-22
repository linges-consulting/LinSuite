"""Where a date-of-birth change will drive retention recompute (Task 4, #39).

A single writer, called from the one place `customers/routes.py` changes a date of birth,
so Task 4 hooks its recompute in here rather than touching the PATCH route. A no-op today —
`retention_expires_at` does not exist yet — but the call site does, so it never needs adding
under time pressure alongside a DOB edit.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from customers.models import Customer


async def on_dob_changed(db: AsyncSession, customer: Customer) -> None:
    """No-op in Task 3. Task 4 recomputes `retention_expires_at` here."""
