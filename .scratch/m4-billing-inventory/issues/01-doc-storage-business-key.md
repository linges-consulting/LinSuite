# 01 — Financial document storage: business-owned key tier (ADR-0003)

**What to build:** A second document-encryption key tier, owned by the business rather than
by any one customer, so an invoice or receipt can be stored, retrieved and retained
independently of that customer's own clinical crypto-shred — and so a document with no
customer at all (an anonymous retail sale) has somewhere to be encrypted under. Draft and
land ADR-0003 documenting the new tier alongside ADR-0001, rather than silently reinterpreting
it. This is additive (the existing customer-keyed tier is untouched), so no expand/contract
migration is needed — existing documents and their callers keep working exactly as before.

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] `Document.customer_id` is nullable; a document may be created with no customer at all
- [ ] A new business-level key (wrapped under `DOCUMENT_MASTER_KEY`, no per-customer layer)
      encrypts every billing-owned document; `store_document`/`fetch_document` gain an
      explicit owner parameter (customer key vs business key) rather than inferring it
- [ ] Existing customer-keyed documents (forms, session notes) are unaffected — same behavior,
      same tests passing, no migration of existing rows
- [ ] Crypto-shredding a customer's own document key never touches a business-keyed document,
      even when that document is linked to that customer via `customer_id`
- [ ] Financial retention (the CRA 6-year rule) is tracked and enforced independently of
      clinical retention — a business-keyed document is never purge-eligible merely because
      the linked customer's clinical hold has expired
- [ ] ADR-0003 is written and committed, describing the two-tier ownership model and why it
      does not weaken ADR-0001's guarantees
- [ ] `tests/test_schema.py`'s grant/trigger sweep passes against the new table/columns
