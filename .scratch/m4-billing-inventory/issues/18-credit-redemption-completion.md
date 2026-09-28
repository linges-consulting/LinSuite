# 18 — Credit redemption at appointment completion

**What to build:** Staff select an eligible, fully-paid package/bundle before completing an
appointment; completion (#05's same transaction) redeems one credit as a guarded, race-safe
append-only movement, and the draft bill line shows the visit as prepaid — no new amount to
collect, not a discount applied to a charge. Booking and no-show never touch entitlement; only
completion does. (#54 stories 55-59.)

**Blocked by:** 17 (a paid, active package to redeem from), 05 (the completion hook to redeem
inside)

**Status:** ready-for-agent

- [ ] Staff can select an eligible package/bundle for an appointment before completing it
- [ ] Completion redeems exactly one credit in the same transaction as #05's draft-line
      creation, writing an append-only redemption movement
- [ ] The resulting draft line shows the visit as prepaid (its frozen allocated value), not as
      a new charge to collect
- [ ] Booking or cancelling never deducts a credit; only a completed appointment does
- [ ] Two concurrent completions racing to redeem the last credit result in exactly one
      successful redemption, never a negative balance
- [ ] The treatment receipt for a redeemed visit (once #16 exists) shows the allocated value
      and the actual service date, not a new payment
