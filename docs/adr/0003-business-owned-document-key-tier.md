# ADR-0003: A Business-Owned Document Key Tier

- **Status:** Accepted
- **Date:** 2026-09-28

## Context

Every document sealed so far (signed consents, waivers, intake forms) belongs to exactly one client, and `documents.customer_id` is a required foreign key into `customer_document_keys` — deleting that key row is the crypto-shred (ADR-0001 §5), and the FK is what serialises a purge against a document insert (ADR-0001 rule 7).

M4 adds financial documents — invoices and treatment receipts — that this model does not fit, for two reasons.

**An invoice must outlive the customer's own clinical crypto-shred.** CLAUDE.md's domain rules are explicit: invoices are `voidable`, retained for the CRA's six-year rule, never edited in place. A customer's chart, by contrast, is retained under ADR-0001's clinical formula (`max(last_entry + 10y, dob + 18y + 10y)`) and can be erased once that hold expires. The two clocks are unrelated, and a chart that expires first must not take its invoices with it — nor may an invoice's continued existence indefinitely block a customer's legitimate erasure. Sealing an invoice under the *customer's* key, or FK'ing it to `customer_document_keys` the way a form is, binds its lifetime to the wrong clock in both directions.

**Not every financial document has a customer at all.** An anonymous retail sale (CLAUDE.md, out-of-scope list aside — walk-up retail with no client record) has nothing to seal it under in the current model, which requires `customer_id`.

## Decision

**1. A second key tier, owned by the business, not any one customer.**

`business_document_keys` holds exactly one row (`business_id` is always `1`, the same value `businesses.id`'s own CHECK pins it to) — the deployment's single financial-document key, wrapped under `DOCUMENT_MASTER_KEY` directly, with no per-customer layer. `billing/keys.py`'s `business_key(db)` mirrors `customers/keys.py`'s `data_key`: created lazily on first use, read on every use after.

**2. `documents.customer_id` becomes nullable, and a new `key_owner` column names the tier explicitly.**

`key_owner` is `'customer'` (the original tier, default, entirely unchanged behaviour) or `'business'`. A CHECK ties the two together: `'customer'` requires `customer_id`; `'business'` requires it to be NULL. `core.documents.store_document`/`fetch_document` take `key_owner` as an explicit, required parameter — never inferred from whether `customer_id` happens to be set, because a caller that forgot to pass it would otherwise silently seal a financial document under the wrong assumption.

**3. A business-keyed document's reference to a customer is a different column, with no locking behaviour.**

`linked_customer_id` references `customers.id` directly — never `customer_document_keys` — and carries no FK-driven lock the way `customer_id` does for the customer tier. This is the one design point worth spelling out, because the obvious alternative (reuse `customer_id` for both tiers, just relax its FK target) does not work:

`customer_id`'s FK to `customer_document_keys(customer_id)`, with no cascade, is *only* there for the lock ADR-0001 rule 7 describes — inserting a row takes `FOR KEY SHARE` on the key row, so a purge deleting that key serialises against it. If a business-keyed invoice populated the same `customer_id` column to record which client it was for, that FK would still apply: the invoice would hold a lock reference to that client's key row for as long as the invoice exists (six years), which would either block that client's legitimate crypto-shred for six years, or — worse — get deleted along with the client's other sealed rows the day `customers/tasks.py::_shred` runs its `DELETE FROM documents WHERE customer_id = :c`, destroying a record the CRA requires kept. Both outcomes are exactly what this ADR exists to prevent.

`linked_customer_id` avoids both: it references `customers.id`, a row erasure never deletes (ADR-0001's purge anonymises the profile, it does not remove the row), so a business-keyed document referencing a customer survives that customer's own key being destroyed, unconditionally — and `_shred`'s `DELETE FROM documents WHERE customer_id = :c` never matches a business-keyed row in the first place, because `customer_id` is always NULL on one.

**4. Financial retention is its own column, not derived from the customer's hold.**

`documents.retain_until`, nullable, is a business-keyed document's own CRA-clock expiry, set by its caller. It is independent of `customers.retention_expires_at` by construction — nothing reads the customer's hold to decide a business-keyed document's fate. No purge job reads `retain_until` yet; that is a later ticket's work. This one only makes sure the value has somewhere to live from the first financial document on, so that ticket needs no further migration.

**5. The business key never shreds, in v1.** Unlike a customer's key, `business_document_keys` has no purge-eligible branch: `business_document_keys_guard` refuses UPDATE and DELETE to every runtime role, unconditionally, table-owner only. There is nothing here to make it "held" or "not held" against — it is the deployment's own key, not a client's, and destroying it would make every financial document unreadable at once, which no acceptance criterion of this ticket calls for.

**6. Additive.** No existing row changes tier, no existing column's meaning changes, and every existing caller (`forms/tasks.py`, `forms/scans.py`, `forms/submissions.py`) now passes `key_owner="customer"` explicitly — a one-line, behaviour-preserving addition at each call site, not a migration of data.

## Consequences

**Positive**

- A customer's crypto-shred and a business's financial-record retention are now genuinely independent obligations, matching CLAUDE.md's own framing of the two as separate document classes with separate retention rules.
- An anonymous retail sale has somewhere to be sealed, with no customer required.
- The existing customer-keyed tier (forms, and later session notes) is untouched: same FK, same lock, same guard, same tests.

**Negative**

- Two columns (`customer_id`, `linked_customer_id`) can both notionally "point at a customer" on the same table, for different tiers, which is one more thing a future reader has to keep straight. The CHECK constraints make the invariant machine-checked (a customer-keyed row never sets `linked_customer_id`; a business-keyed row never sets `customer_id`), but the two-column shape is real complexity, accepted because collapsing them reintroduces the exact coupling this ADR exists to avoid.
- `retain_until` is written but not yet enforced by any purge path — a business document is safe from a customer's shred today, but nothing yet deletes one once its own CRA clock expires. Tracked, not yet acted on.

**Neutral**

- `business_document_keys` being a single, permanent, unshreddable row means master-key rotation (already out of scope, per `customers/keys.py`) is the only way this key is ever replaced — consistent with the rest of the system's stance on rotation today.

## Alternatives considered

- **Reuse `customer_id` for both tiers, relaxing its FK to `customers.id`.** Rejected in §3 above: it would either block a customer's crypto-shred for as long as an unrelated invoice exists, or delete that invoice as a side effect of the shred — the customer-keyed tier's existing FK-as-lock mechanism (ADR-0001 rule 7) cannot be reused for a reference that must survive the very deletion that mechanism exists to serialise against.
- **Wrap the business key per financial-document-type instead of once.** Rejected: no requirement calls for it, and it would be a second axis of key management for no isolation benefit a single-tenant deployment needs — there is one business, so one key.
- **Infer `key_owner` from whether `customer_id` is set.** Rejected: a caller bug (forgetting to pass `customer_id` on what should have been a customer-keyed document) would silently reclassify the document as business-keyed instead of failing loudly. An explicit, required parameter fails at the call site instead.
- **A `retention_profile`-style enum reused from `businesses`.** Rejected: `retain_until` is a per-document date, not a per-deployment policy choice — the two `Business.retention_profile` values govern *customer* retention, and financial retention is a fixed statutory period, not a business-configured one.

## Related

- [[0001-retention-expiry-and-purge-authority]] — the crypto-shred and rule 7's FK-as-lock mechanism this ADR deliberately does not extend to the business tier.
