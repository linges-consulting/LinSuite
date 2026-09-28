# 13 — Payment correction + admin-approved refund

**What to build:** Two distinct actions on an existing payment entry: staff correcting a
clerical mistake (wrong amount/method/reference/payer) with a required reason, which preserves
the original and links the correction; and an actual refund, which always requires admin/owner
approval and is capped at payments received minus prior refunds. Correction never moves money
back to the client by itself. (#54 stories 43-45, 62-65.)

**Blocked by:** 12 (payments to correct or refund)

**Status:** ready-for-agent

- [ ] Staff can correct a payment entry's amount/method/reference/payer with a required
      reason; the original entry is preserved, the correction is linked to it, and the balance
      recomputes from the corrected ledger
- [ ] A correction alone never authorizes returning money to the client — that's only ever a
      refund
- [ ] An admin/owner-approved refund records amount, reason and approver; the total of all
      refunds against an invoice (and its replacement lineage, once #14 exists) can never
      exceed payments actually received
- [ ] Concurrent correction and refund attempts against the same invoice cannot together
      violate the refund cap or leave an inconsistent balance
- [ ] Staff attempting a refund directly (not a correction) is refused — refund is
      admin/owner-only
