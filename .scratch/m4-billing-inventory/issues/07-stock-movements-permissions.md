# 07 — Stock movements + receiving/adjustment permissions

**What to build:** An append-only stock movement ledger (receipt, sale, return, manual
adjustment), each retaining its actor, reason and signed whole-unit change, with receiving and
manual count-correction as two separate, server-enforced permissions — both defaulting to
admin/owner and independently grantable to trusted staff. Ordinary sale-driven deductions
(#21) never require either permission. Stock can never go negative. (#54 stories 78-80.)

**Blocked by:** 02 (needs a product/variant to move stock against)

**Status:** ready-for-agent

- [ ] Every stock change (receipt, sale, return, manual adjustment) writes a movement row:
      signed whole-unit delta, actor, reason (required for manual adjustments), timestamp,
      and which kind of movement it was
- [ ] `inventory.receive` and `inventory.adjust` are distinct capabilities, checked server-side
      on their respective endpoints, both defaulting to admin/owner only
- [ ] A sale-driven deduction (once #21 exists) never needs either permission
- [ ] Stock quantity is guarded at the database level against going negative under concurrent
      writes, not only checked in application code
- [ ] Movement history is retained permanently — it is never overwritten or replaced with a
      single mutable "current stock" log
