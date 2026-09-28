# 11 — Invoice issue (service bills)

**What to build:** Turning a reviewed, approved draft into an issued invoice: an atomically
allocated, gapless business invoice number, an immutable snapshot of every line's amount,
discount, tax and attribution, and a hard refusal to issue a draft carrying an unresolved or
stale approval request. Once issued, nothing reads amounts by recomputing from live catalog/
discount/tax definitions — only from the snapshot. (#54 stories 10-11, 29, 33; "Invoice issue"
implementation-decision bullet.)

**Blocked by:** 10 (a bill that has gone through whichever review path it needed)

**Status:** ready-for-agent

- [ ] Issue is refused if the draft carries a pending or stale approval request, or if
      required admin authorization hasn't actually happened
- [ ] The invoice number is allocated atomically at issue, gapless per business, and survives
      concurrent issue attempts without a collision or a gap
- [ ] Every line's amount, discount, tax breakdown and commission-basis choice is copied into
      the issued record at that instant — a later definition change never alters it
- [ ] Reading an issued invoice never recomputes from the live discount/tax/catalog tables
- [ ] The database enforces the invoice's issued fields as append-only/immutable, not only the
      application layer (grants/triggers, following the existing document-immutability pattern)
