# 14 — Cancel & replace issued invoice

**What to build:** Staff can cancel an issued invoice with a required reason; cancellation
atomically marks the original cancelled (never deleted, never overwritten — CRA needs a
continuous invoice number sequence) and creates a linked replacement draft pre-filled from the
original. Existing payments carry over to the replacement with a transfer history; any
increase is collected as an ordinary new payment, any decrease routes through #13's approved
refund. Cancellation never duplicates stock, credit or commission effects, and never implies
the underlying delivered service or goods were physically returned. (#54 stories 46-51.)

**Blocked by:** 12 (payments to carry over), 11 (an issued invoice to cancel)

**Status:** ready-for-agent

- [ ] Cancelling an issued invoice requires a reason, retains the original document and its
      invoice number, and marks it cancelled (never deleted)
- [ ] A replacement draft is created automatically, pre-filled from the original's details, and
      linked to it (lineage preserved, queryable both directions)
- [ ] Existing payments transfer to the replacement with a recorded transfer history — the
      client is never charged again for money already collected
- [ ] A price increase on the replacement is collected as an ordinary new payment; a decrease
      requires #13's admin-approved refund, not an automatic one
- [ ] Cancellation never deducts a package credit or stock unit a second time, never allocates
      the same payment twice, and never counts original-plus-replacement commission together
- [ ] A retried cancel/replace request produces one lineage and one set of effects, not two
