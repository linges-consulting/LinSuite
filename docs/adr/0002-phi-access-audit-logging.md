# ADR-0002: PHI Access (Read) Audit Logging

- **Status:** Accepted
- **Date:** 2026-09-17

## Context

The PRD calls for tamper-proof audit logging of "role activities and record modifications." That framing covers writes: who changed what, and when.

It is insufficient for two reasons.

**PHIPA requires a log of who viewed a record.** Ontario health information custodians must be able to produce an electronic audit log showing access to personal health information, not merely modification of it. The canonical scenario the requirement exists for — a staff member browsing the chart of a neighbour or a public figure they have no clinical relationship with — leaves no trace at all in a write-only log, because nothing was written.

**Breach scoping is impossible without read logs.** When PIPEDA's mandatory reporting obligation is triggered, the Privacy Commissioner and the affected individuals need to know *which* records were exposed. If a staff account is compromised, a write-only log shows what the attacker changed, which is typically nothing. The question that must be answered — what did they look at — has no source of truth.

## Decision

**1. Record access events, not just mutations.**

`audit_access_log` captures actor, actor role, customer, resource type, resource id, action, source IP, and timestamp, for views of customer profiles, session notes and archived form documents.

It stores identifiers only. The content viewed is never copied into the log, which would otherwise duplicate the personal health information into a second table with different retention.

**2. Instrument routes explicitly, not by middleware.**

Access logging is a FastAPI dependency applied per route on endpoints that return PHI. It is deliberately not middleware that infers which responses contain PHI: inference silently omits endpoints added later, and the omission is discovered during an audit rather than during development. An explicit dependency makes the decision visible in the route definition and reviewable in a diff.

**3. Write inside the request transaction.**

Not batched, not fire-and-forget. A dropped entry is precisely the entry that matters.

**4. Log at record open, not at list render.**

Opening a client's chart is an access event. Rendering a day's calendar that happens to display forty client names is not. Logging list renders would write tens of rows per page load and bury genuine access events in noise, producing a log that satisfies the letter of the requirement while being useless for the purpose it exists to serve.

**5. Partition yearly from day one.**

This is the highest-volume table in the system, and it inherits the ten-year retention horizon. Partition creation is automated; next year's partition must exist before January or writes fail.

**6. Surface it as a report.**

An admin screen answers "who accessed this client's record," which serves the statutory audit duty, a client's own access request, and an internal investigation with the same query.

## Consequences

**Positive**

- Unauthorised browsing of records becomes detectable, which is the behaviour the PHIPA requirement targets.
- Breach notification can state which records were accessed rather than which were modified.
- The same data answers client access requests without additional work.

**Negative**

- A synchronous insert on every chart open adds a write to read-heavy paths. Expected volume is roughly 300–600 events per day for a busy practice — on the order of 150,000 rows per year, which Postgres handles without strain — but the write is on the critical path of the most frequently used screen in the product.
- Explicit instrumentation can be forgotten on a new endpoint. This is a real risk, accepted in exchange for the failure being visible in code review rather than invisible in middleware. A test that asserts every PHI-returning route carries the dependency would close it.
- Partition maintenance is an operational obligation. A missed partition causes write failures, which — because logging is synchronous and in-transaction — become user-visible errors on chart open.

**Neutral**

- The line between "open" and "render" is a judgement call that may need revisiting if a screen is built that effectively discloses a chart without opening it.

## Alternatives considered

- **Write-only audit log (the original PRD design).** Rejected: does not satisfy PHIPA, and cannot scope a breach.
- **Middleware that logs all responses containing customer data.** Rejected: silently misses endpoints, over-logs list views, and hides the compliance decision from code review.
- **Asynchronous or batched logging via Celery.** Rejected: the failure mode is losing exactly the entries that matter, during the incident that makes them matter.
- **Logging list renders as access.** Rejected: produces a log too noisy to audit, which defeats the purpose while appearing compliant.

## Related

- [[0001-retention-expiry-and-purge-authority]] — purge events are written to the audit log, and the access log inherits the same ten-year retention horizon and partitioning approach.
