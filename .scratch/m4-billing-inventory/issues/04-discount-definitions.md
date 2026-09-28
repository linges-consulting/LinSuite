# 04 — Discount definitions

**What to build:** Admin-defined, reusable discounts — fixed-amount or percentage, eligible
against all or selected services/products/packages, a stackable flag, and an explicit
commission-basis choice (reduces commission basis, or absorbed by the business) per discount.
No application logic yet (#09 consumes these); this ticket is the definition CRUD and its
resolved-amount math as a pure, independently testable function. (#54 stories 18, 19, 21, 25.)

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] Admin can define a discount: fixed or percentage, enabled/disabled, eligibility
      (all-or-selected items across services/products/packages), stackable (bool), and its
      commission-basis choice
- [ ] A pure function resolves a set of stacked discounts against a charge deterministically:
      all percentage discounts apply sequentially first, then fixed amounts, independent of
      selection order (100 → 10% → 20% = 72, not 70) — S2-tested with an independently
      computed expected value, not a mirror of the implementation
- [ ] Two discounts are combinable only if both are marked stackable; a non-stackable conflict
      is rejected with a clear reason, not silently resolved
- [ ] A combination that would exceed the eligible charge is rejected, never silently clamped
      to zero
- [ ] Editing a discount's definition later never changes an already-issued invoice's resolved
      amounts (proven once #11 exists to issue against — note the dependency for that test,
      but this ticket's own tests only need the pure resolver)
