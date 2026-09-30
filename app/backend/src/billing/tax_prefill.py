"""Tax pre-fill (#118, spec #113 "Tax pre-fill"): what `settings/routes.py::update_business`
calls after saving the profile.

**Fires at most once, ever, per business.** The gate is "does `tax_components` have any row at
all" — not "any *active* row", not "any row for this province" — because the only question this
function answers is "has anybody, ever, touched this business's tax configuration." An owner
who typed one component by hand, then deactivated it, still gets nothing created here: their
row already answered the question. If the gate passes, every component `billing/tax_table.py`
lists for the saved province is created, each with its *own* current rate and that rate's own
`effective_from` (not today) — spec story 27, "invoices snapshot the rate that applied on the
day." All of it, plus the audit event, happens in the caller's transaction: `update_business`
commits once, so the profile and the pre-fill either both land or neither does.

Nothing here ever updates or deletes a `TaxComponent` — see that class's own `origin` column
docstring (`billing/models.py`) for why a second run, or a later province change, is always a
no-op here and a prompt on the Tax panel instead (`billing/tax_routes.py`), never a silent edit.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from billing.models import TaxComponent, TaxComponentRate
from billing.tax_table import components_for_province
from core.audit import record_event
from core.models import Business
from scheduling.clock import today_in


async def prefill_if_eligible(
    db: AsyncSession, business: Business, *, actor_user_id: uuid.UUID
) -> None:
    """Called with the profile save already staged on `business` (not yet committed). A no-op
    unless `business.province` is set and the business has no tax components at all."""
    if business.province is None:
        return
    already_configured = await db.scalar(select(TaxComponent.id).limit(1))
    if already_configured is not None:
        return

    today = today_in(business.timezone)
    created_codes: list[str] = []
    for table_component, rate in components_for_province(business.province, today):
        component = TaxComponent(
            code=table_component.code,
            name=table_component.name,
            province=business.province,
            active=True,
            origin="prefill",
        )
        component.rates = [
            TaxComponentRate(rate_ppm=rate.rate_ppm, effective_from=rate.effective_from)
        ]
        db.add(component)
        created_codes.append(table_component.code)

    if not created_codes:
        # No table entry covers this province as of today (a territory or province this table
        # has no rate history reaching back to, which none of the thirteen currently do) —
        # nothing to create, and no audit event for a pre-fill that did not happen.
        return

    await db.flush()
    record_event(
        db,
        "billing.tax_prefilled",
        target_type="business",
        target_id=str(business.id),
        actor_user_id=actor_user_id,
        metadata={"province": business.province, "codes": created_codes},
    )
