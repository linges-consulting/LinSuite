# ADR-0001: Retention Expiry and Purge Authority

- **Status:** Accepted
- **Date:** 2026-09-17

## Context

The PRD requires that signed consents, waivers and intake forms be immutable, and that records be retained for a minimum of ten years. The initial design satisfied immutability by revoking `UPDATE` and `DELETE` from the application role on those tables and enforcing the same with triggers.

Two findings made that design incomplete.

**Retention is not optional in one direction only.** PIPEDA grants no general right to erasure — it requires destruction when data is *no longer necessary for the purpose for which it was collected*. The Consumer Privacy Protection Act, which would have introduced a genuine disposal right, died with Bill C-27 in July 2026 with no successor enacted. But the existing obligation still cuts both ways: retaining personal information indefinitely is itself a violation. A table that nothing can ever delete from is therefore non-compliant, not maximally compliant.

**The retention period depends on date of birth, not on the last visit.** Ontario's regulated health colleges require clinical records to be kept for ten years from the last entry in the record, *or* until ten years after the client turns eighteen — whichever is later. A record opened for an eight-year-old must be retained until they are twenty-eight. A fixed offset from the last appointment produces the wrong date for every minor.

Meanwhile, businesses using this product are not uniformly regulated. A massage clinic has a statutory retention obligation; a hair salon or a business coach does not, and for them a client's deletion request should simply be honoured.

## Decision

**1. Retention profile is per-business configuration.**

Each deployment is configured as `regulated_health` or `general_business`. Retention periods are configuration rather than constants, because each client must confirm them against their own college or counsel.

**2. Retention expiry is computed and stored at the customer level.**

For regulated profiles, `customers.retention_expires_at = max(last_clinical_entry_at + 10y, date_of_birth + 18y + 10y)`.

It is stored rather than computed at query time, so the purge job is an indexed date scan. It is held at the customer level rather than per document because the obligation attaches to the chart as a whole — every new session note extends the retention of the entire record. Per-document expiry values would drift apart and result in purging half a chart.

The value is recomputed when a clinical entry is inserted and when a date of birth is corrected.

**3. A deletion request never deletes records under a live retention hold.**

It creates an `erasure_request`, immediately purges what is not held — marketing preferences, contact details beyond the record, non-clinical notes — and suppresses the profile from active views. The client is told what is retained and why. Held records are purged automatically once expiry passes.

**4. Deletion authority belongs to a separate database role.**

- `linsuite_app` — the application role. `DELETE` remains revoked on immutable tables.
- `linsuite_purge` — a distinct role with a distinct DSN and `DELETE` granted, used only by the retention expiry job through a second SQLAlchemy engine.

The trigger on immutable tables permits deletion only when `current_user = 'linsuite_purge'` **and** `retention_expires_at < now()`. Requiring both conditions means a leaked purge credential still cannot erase records that are inside their retention window.

**5. Documents are crypto-shredded.**

Each customer has a data encryption key in `customer_document_keys`, wrapped by the deployment master key. Purging destroys the key rather than rewriting partitioned tables, which makes erasure cheap and verifiable.

**6. Every purge writes to the audit log** — the fact of erasure and its authority, never the erased content.

## Consequences

**Positive**

- The system can satisfy both the obligation to retain and the obligation to destroy, which a purely append-only design could not.
- Minors' records are retained correctly without manual tracking.
- Compromising the application grants no ability to destroy records, because the application genuinely lacks the privilege — this is enforced by the database, not by code review.
- Non-regulated businesses get prompt, honest deletion rather than being subjected to a clinical retention model that does not apply to them.

**Negative**

- Two database roles and two engines in `core/` is more configuration than a single connection, and the purge DSN is an additional secret to manage and escrow.
- `retention_expires_at` is denormalised state that must be recomputed correctly. A missed recompute silently shortens or extends retention, and the error is invisible until an audit or a premature purge. This path needs a test.
- Crypto-shredding means a lost or corrupted key row is indistinguishable from an intentional erasure. Key handling is already the system's most dangerous failure mode (see the escrow requirement in `docs/tech-stack.md`).

**Neutral**

- Retention periods being configuration means the vendor does not warrant their correctness; each client confirms them with their own regulator.

## Alternatives considered

- **Append-only with no purge path.** Rejected: indefinite retention is itself a PIPEDA violation, and the design would have no way to honour a legitimate deletion request even for non-regulated businesses.
- **Application-level soft deletes.** Rejected: a `deleted_at` flag leaves the personal information in the database, which does not satisfy a destruction obligation.
- **Fixed retention offset from the last appointment.** Rejected: produces the wrong expiry for every client who was a minor at the time of service.
- **Allowing the application role to delete after checking expiry in code.** Rejected: correctness would then depend on every future code path performing the check, and a compromised application could erase records at will.
