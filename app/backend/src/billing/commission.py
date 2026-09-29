"""Commission-basis composition (#69, S2): the pure arithmetic that turns one invoice line's
pre-discount price, the discounts that applied to it, and the commission rate already
snapshotted at completion (#59) into the commission amount actually owed to the delivering
staff member.

No database, no ORM — the same seam `billing/tax.py`/`billing/discount_resolver.py`/
`billing/allocation.py` already establish, and for the same reason: rounding and composition
rules belong in a plain-pytest-tested function, not re-proven through an HTTP round trip.

**Never reads `Staff.commission_rate_services_bp` live.** The rate is always handed in by the
caller, already snapshotted at appointment completion (`ServiceBillLine.commission_rate_bp`,
#59) and frozen again onto `InvoiceLine.commission_rate_bp` at invoice issue (#65). This
module only does arithmetic on numbers it is given — CLAUDE.md's "Commission earns on
delivery... rates are snapshotted on invoice lines so rate changes don't rewrite history."

**The composition formula, exactly.** `InvoiceLineDiscount.commission_basis` (frozen at issue,
#65) says, per applied discount, whether it `"reduces"` the commission basis (calculated on
what the client actually paid, after that discount) or is `"absorbed"` by the business
(calculated as though that discount never happened). With more than one discount on a line,
their dollar contributions are not independently recoverable: `discount_resolver.
resolve_stacked_discounts` combines every percentage discount as a *product* of each one's
`(1 - rate)`, not a sum, so "how many cents did just this one discount take off" has no single
right answer once two percentage discounts stack. Rather than approximate that split, this
module sidesteps the question: it re-runs the exact same stacking function against the
original `price_cents`, but with every `"absorbed"` discount left out of the input set
entirely.

- A `"reduces"`-only set reproduces exactly `discounted_cents` (the amount already charged) —
  commission is calculated on what the client paid.
- A `"absorbed"`-only set reproduces `price_cents` unchanged (`resolve_stacked_discounts` on an
  empty list is a no-op) — commission is calculated as if the discount never applied, because
  the business is the one who is out that money, not the staff member.
- A mixed set nets out to whatever the reduces-only subset computes, using the resolver's own
  percentage-then-fixed, half-up-once composition — never a second, hand-rolled formula.

**Never raises `DiscountConflict`.** The reduces-only subset is provably never a *larger*
reduction than the full set `bill_review.py::compute_bill` already validated successfully when the
bill was issued: removing any discount from a stack can only raise the resulting charge
(fewer/smaller factors and fewer fixed subtractions), and a subset of an already-mutually-
stackable set is still mutually stackable (the "more than one non-stackable" rule only ever
gets *less* true as items are removed). See `tests/test_billing_commission.py` for the
worked proof.

**Package-service commission constraint (for #72), documented here because this is where a
future caller would look for it.** `staff_id` is not even a parameter of this module — that is
deliberate. The caller (`billing/invoices.py::issue_invoice`) is responsible for posting a
`CommissionPosting` row against the *delivering* staff member's id — `InvoiceLine.staff_id`,
itself copied from `ServiceBillLine.staff_id`, itself copied from `Appointment.staff_id` at
completion (#59) — and never against whoever originally sold a package the appointment
redeemed a credit from. Nothing in this module enforces that; it is a contract on whatever sets
`ServiceBillLine.staff_id` (today, `scheduling/appointments.py::complete_appointment`; once
#72 exists, credit redemption at completion must keep setting it to the appointment's own
staff member, never the package's original purchaser).
"""

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from billing.discount_resolver import DiscountInput, resolve_stacked_discounts

BASIS_POINTS = 10_000

CommissionBasis = Literal["reduces", "absorbed"]
Kind = Literal["percentage", "fixed"]


@dataclass(frozen=True)
class CommissionDiscountInput:
    """One discount that applied to the line, with the one fact beyond `discount_resolver.
    DiscountInput` this module's formula needs: `commission_basis`. Read off `InvoiceLineDiscount
    .commission_basis` at posting time (the value frozen at invoice issue, #65) — never off the
    live `Discount` row, so a later basis change on the definition can never rewrite an
    already-earned commission."""

    id: uuid.UUID
    kind: Kind
    stackable: bool
    percentage_bp: int | None
    amount_cents: int | None
    commission_basis: CommissionBasis


def commission_basis_cents(price_cents: int, discounts: list[CommissionDiscountInput]) -> int:
    """The amount commission is calculated on for one line — see the module docstring for the
    full formula and why it re-runs the stacking resolver rather than netting dollar amounts."""
    reduces_only = [
        DiscountInput(
            id=d.id,
            kind=d.kind,
            stackable=d.stackable,
            percentage_bp=d.percentage_bp,
            amount_cents=d.amount_cents,
        )
        for d in discounts
        if d.commission_basis == "reduces"
    ]
    return resolve_stacked_discounts(price_cents, reduces_only)


def _round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def commission_amount_cents(basis_cents: int, commission_rate_bp: int) -> int:
    """The rate — already snapshotted, never re-read live — applied to an already-computed
    basis. Half-up, `billing/tax.py`'s own rounding convention (CLAUDE.md "Money is integer
    cents... half-up")."""
    return _round_half_up(Decimal(basis_cents) * commission_rate_bp / BASIS_POINTS)


def compute_commission_cents(
    *, price_cents: int, commission_rate_bp: int, discounts: list[CommissionDiscountInput]
) -> int:
    """The end-to-end convenience wrapper: `commission_basis_cents` then `commission_amount_
    cents`. `billing/invoices.py` calls the two steps separately instead (it needs to freeze
    `basis_cents` onto the ledger row too), but this is what a caller who only wants the final
    number — and every test that just wants a worked answer — should reach for."""
    basis = commission_basis_cents(price_cents, discounts)
    return commission_amount_cents(basis, commission_rate_bp)
