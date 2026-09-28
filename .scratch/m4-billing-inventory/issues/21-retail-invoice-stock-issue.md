# 21 — Retail invoice: draft → atomic stock-deducting issue

**What to build:** A retail sale, always on its own invoice (never combined with services).
A draft leaves stock untouched; issuing the invoice atomically revalidates availability and
deducts stock via #07's movement ledger in the same transaction as invoice issue — two
concurrent buyers can never both take the last unit. Sales can be anonymous or linked to an
existing client. "Sold by" defaults to whoever is handling the sale but is staff-selectable,
recorded separately from whoever collects payment. (#54 stories 5, 69-77.)

**Blocked by:** 07 (stock movements to deduct into), 11 (the shared invoice/payment machinery,
parameterized for a retail line source instead of a service completion)

**Status:** ready-for-agent

- [ ] A retail draft never deducts or reserves stock — building the bill has no stock effect
- [ ] Issuing the retail invoice atomically revalidates availability and deducts stock,
      writing the movement in the same transaction as the invoice; an unavailable quantity is
      rejected at issue even under concurrent attempts
- [ ] Two simultaneous issue attempts for the last unit result in exactly one success
- [ ] A retail sale can proceed with no customer at all, or linked to an existing client (then
      visible in their history)
- [ ] "Sold by" defaults to the acting staff member but is selectable; the payment collector
      is recorded separately and never changes "Sold by" attribution
- [ ] A failure at any point leaves invoice, stock, movements and payments in a consistent
      state — no partial deduction with no invoice, or vice versa
