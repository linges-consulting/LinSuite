# Technology Stack

Decisions are final unless a documented trigger says otherwise. Each entry records *why*, so a future reader can tell whether the reasoning still holds.

## 1. Backend API & Core Logic: **Python (FastAPI)**

* **Why:** FastAPI is asynchronous, fast, and natively supports modern Python type hinting. Its dependency injection system fits RBAC permissions, database sessions, and Admin/Staff context-switching cleanly.

### Authentication: self-rolled JWT

* **Decision:** Build auth in-house — FastAPI dependencies for the session/permission layer, `argon2-cffi` for password hashing (passlib's last release predates Argon2id being the default, and it adds a wrapper over the same library), a JWT library for token issue/verify. No third-party IdP (Auth0, Clerk), no auth framework (`fastapi-users`).
* **Why:** The PRD's dual-mode sliding-window session policy (Admin 15–30 min hard timeout, Staff long-lived, explicit context switching between them) is a custom state machine. Off-the-shelf providers and libraries model a single session lifecycle, so the policy would have to be bolted on around them — more work than writing it directly, and harder to reason about during a security review.

## 2. Frontend User Interface: **React + Vite + Tailwind CSS + shadcn/ui**

* **Component library — shadcn/ui (Radix primitives + Tailwind), not Ant Design.** White-label branding is the product's core premise: with Tailwind CSS variables, swapping a business's primary/secondary HEX is a single token set. Ant Design ships its own `ConfigProvider` token system, which would mean maintaining two theming systems in sync across every branded surface. Neither library provides a resource-scheduling calendar, so Ant Design's "batteries included" advantage largely disappears on the hardest screen.
* **Data grids:** TanStack Table (inventory grids, customer lists, reports).
* **Scheduling calendar:** a dedicated calendar library is required for color-coded multi-staff schedules. **Not yet selected** — see the open question in "Unresolved" below.
* **Server state:** TanStack Query. Almost all state in this app is server state (appointments, customers, stock, form status), and TanStack Query already provides caching, background refetch and optimistic updates. No Redux or Zustand — a global store would mostly re-implement a worse cache.
* **Client state:** React Context for the auth session and the Admin/Staff mode switch. Nothing more.

### Language: English only

* **Decision:** No i18n plumbing in v1.
* **Why:** The surface that would legally require French under Quebec's Bill 96 is small (booking portal, notification templates, intake forms — tens of strings); the admin/staff UI is internal and stays English regardless. Retrofitting `react-i18next` over that surface later is roughly a day's work, so paying the plumbing tax on every string today is a poor trade.
* **Trigger to revisit:** a Quebec client entering scope.

## 3. Database & Caching: **PostgreSQL + Redis**

* **PostgreSQL:** ACID guarantees for financial records, inventory counts, and appointment booking locks; JSONB for custom intake form payloads.
* **ORM & migrations:** SQLAlchemy 2.0 (async) + Alembic. SQLAlchemy 2.0's typed API fits FastAPI's style, and Alembic is needed for hand-written SQL in migrations — table partitioning, append-only triggers, and permission `REVOKE`s. Not SQLModel (lags SQLAlchemy features, awkward once queries stop being simple); not raw `asyncpg` (would mean hand-rolling relationship loading across eight domains).
* **Redis:** caching layer (public booking slot lookups, session state) and Celery message broker.

### Document storage: PostgreSQL `bytea`, not object storage

* **Decision:** PDFs are stored in the database as encrypted `bytea`. No MinIO, no DigitalOcean Spaces, no filesystem storage.
* **Why:** One backup covers everything. With a separate object store, the database and the store must be restored to a consistent point in time or form records point at files that no longer exist — and on-prem customers managing their own backups are exactly the population that gets this wrong. `bytea` also makes the document and its metadata commit in a single transaction, so there are no orphaned files and no cleanup job.
* **Encryption:** AES-256-GCM applied in the application before insert, so the PDF is ciphertext to anyone reading the database or a dump. SHA-256 stored alongside and verified on read.
* **Size expectation:** typed intake PDFs run 200–400 KB; a busy practice generates roughly 2–3 GB/year, about 25 GB over the 10-year retention window — comfortable for PostgreSQL. Scanned paper is the risk: multi-page scans at 3–15 MB would push the same volume past 200 GB. Scans are therefore downsampled (≈150 DPI grayscale) on upload.
* **Trigger to revisit:** database crossing ~100 GB, or a client contractually requiring auditor-verifiable immutable storage. Reads and writes live behind `store_document` / `fetch_document` in `core/`, so moving to object storage is a change in one file.

### Retention: yearly partitioning, not cold-storage tiering

* **Decision:** Heavy tables (appointments, documents, audit log) use native PostgreSQL declarative partitioning by year. Old data stays in the same database.
* **Why:** The requirement's actual goal is keeping operational queries fast. Partitioning achieves that directly — date-filtered queries touch only the current partition — without a second storage system to back up and restore consistently. This removes the archive-tiering background worker from scope entirely.
* **If the database does become too large:** detach an old partition and dump it to an encrypted file. A runbook step, not a service.

## 4. Background Workers & Task Processing: **Celery (Python)**

* **Why:** Runs the notification retry queue (email/SMS with exponential backoff), PDF generation, and compliance exports without blocking API threads.
* **Note:** The archive-tiering worker described in the original PRD is no longer needed — partitioning replaces it.

## 5. Document Generation: **WeasyPrint or ReportLab (Python)**

* **Why:** Renders HTML/CSS templates into print-ready intake sheets, receipts and certificates.

### Immutability is enforced in the database, not in the PDF

PDF permission flags ("locked against editing") are advisory and trivially stripped. Real enforcement comes from the schema:

* `REVOKE UPDATE, DELETE` on immutable document tables from the application role, plus a trigger that raises on both.
* SHA-256 per document, verified on read — tamper evidence.

Documents fall into three classes:

| Class | Documents | Rule |
|---|---|---|
| **Immutable** | Signed consents, waivers, intake forms | Never updated or deleted. 10-year retention. |
| **Voidable** | Invoices, receipts | Never edited in place. Cancelling sets a status and links to the replacement; the original is retained. |
| **Lockable** | Session notes | Mutable until locked, immutable afterwards. |

Cancelled invoices are retained rather than deleted: CRA requires business records for six years, and a gap in invoice numbering with nothing explaining it is worse to an auditor than a voided document.

## 6. Notifications: **Resend (email), Twilio (SMS, opt-in)**

* **Email — Resend.** Free tier covers 3,000 emails/month (100/day) on one verified domain with no injected branding. Each business is its own deployment, so the single-domain limit is not a constraint. Brevo was rejected despite a larger free tier because it stamps its own logo on outgoing mail, which breaks the white-label premise. Postmark's free tier (100/month) is too small to run reminders.
* **Upgrade path:** Amazon SES at ~$0.10 per 1,000 emails, available in the Canada (Central) region, for tenants exceeding 100 emails/day.
* **SMS — admin-configurable, disabled by default.** No free SMS exists; Twilio in Canada runs roughly $0.0083/message plus a $1.15/month number and carrier fees. A business that wants SMS supplies its own Twilio credentials in the admin panel and pays for it. The notification engine treats SMS as an adapter that stays unimplemented until a tenant asks.

## 7. Testing: **pytest (backend), Vitest (frontend)**

* **Backend:** pytest + pytest-asyncio + httpx for an async API test client.
* **Frontend:** Vitest + React Testing Library — pairs natively with Vite, no separate runner config.

## 8. Infrastructure: **Docker Compose + Traefik / Nginx + DigitalOcean or on-prem**

* **Docker Compose:** Each deployment runs an isolated stack — `app`, `db`, `redis`, `worker`, `traefik`.
* **Traefik or Nginx:** Reverse proxy handling SSL/TLS 1.3 termination (Let's Encrypt), routing, and security headers.
* **DigitalOcean:** Standard hosting target, Canadian regions (Toronto/Montreal) for data residency.
* **On-prem:** Supported as a first-class deployment target. The same Compose stack runs on a customer's own server; only volume paths and TLS certificate issuance differ.

### Provisioning: manual runbook, not infrastructure-as-code

* **Decision:** New tenants are provisioned by following a written runbook. No Terraform, Ansible, or Kubernetes.
* **Why:** Each business's stack configuration can differ meaningfully by use case, so a parameterised IaC module would accumulate branches faster than it saves work at current tenant counts.

### On-prem differences the runbook must cover

| | DigitalOcean | On-prem |
|---|---|---|
| Disk encryption at rest | Automatic (DO Volume LUKS AES-256) | Customer's responsibility — otherwise application-level PDF encryption is the only protection |
| Backups | DO snapshots plus offsite restic | Customer must configure; highest-risk gap |
| Disk capacity | Resize volume from console | Physical drive |
| TLS certificates | Traefik + Let's Encrypt | No public DNS, so Let's Encrypt cannot validate — needs an internal CA or self-signed certificate |

## 9. Secrets Management: **`.env` + `pydantic-settings`**

* **Decision:** Per-deployment `.env` file, mode `0600`, never committed. No Vault or cloud secret manager.
* **Why:** Four values on a single-tenant box where anyone with root already controls the application. A secrets service would add an operational dependency — including for on-prem customers — without a matching threat-model gain.
* **Critical:** The PDF encryption key must be escrowed separately from the database backup. If a tenant restores a dump and the key is gone, ten years of consent records are permanently unreadable ciphertext — the backup appears healthy and is worthless. Key escrow is an explicit runbook step performed at install and on rotation.

## 10. Backup & Ransomware Resilience: **restic, nightly, offsite, object-locked**

* **Tool — restic.** Encrypts client-side and supports every backend worth using, so the destination is configuration rather than code: `BACKUP_TARGET=b2|s3|sftp|local`. On-prem customers point it at a NAS over SFTP plus an offsite copy; DigitalOcean tenants point it straight at Backblaze B2.
* **Schedule:** nightly `pg_dump` (includes document `bytea`), 24-hour RPO accepted.
* **Offsite target — Backblaze B2.** 10 GB free, ~$6.95/TB/month after, free egress up to 3× stored, and — unlike DigitalOcean Spaces — it supports **Object Lock**.

### The core protection: the server cannot destroy its own backups

* B2 application key scoped to the bucket with write and read only, **no delete permission**.
* Bucket Object Lock with a 30–90 day retention window, so snapshots cannot be deleted or overwritten inside that window even with stolen credentials.
* Pruning runs monthly from an operator workstation using a privileged key that never exists on a tenant server.
* Encryption keys are escrowed offline, **not** in the restic repository — otherwise an attacker holding both the repo and its password obtains plaintext health records.

| Attack | Mitigation |
|---|---|
| Ransomware encrypts the server | Offsite repository unaffected; rebuild via runbook and restore |
| Stolen credentials used to delete backups | No-delete key plus Object Lock window |
| Backups overwritten with garbage | Object Lock blocks overwrite; prior versions retained |
| Backup repository exfiltrated | restic client-side encryption; PDFs additionally encrypted with a key absent from the repo |
| Slow corruption or logic bomb | 90-day retention reaches back past it; per-document SHA-256 detects tampering |
| Insider deletes consent records | Append-only trigger plus audit log |
| Backups silently stopped months ago | Healthcheck ping after each run; alert if no success in 36 hours |

* **Trigger to revisit:** a tenant for whom a lost day is unacceptable — add continuous WAL archiving for point-in-time recovery (minutes RPO instead of 24 hours).

## 11. Observability: **structured JSON logs**

* **Decision:** Structured JSON logs to stdout, captured by Docker. No Sentry, no GlitchTip, for now.
* **When error tracking is added:** it must run with PII scrubbing enabled. An unhandled exception during form submission can otherwise carry the request body into the error report, shipping a client's health data to a third-party service and breaking both the data-residency commitment and PHIPA.

## 12. CI: **GitHub Actions on pull request**

* `ruff` (lint and format), `pytest`, `vitest`, and a `docker compose build` smoke check. Nothing else.
* No CD pipeline, release automation, or registry push — deployment is the manual runbook, and building that machinery before a second tenant exists is work that rots before use.

## 13. Scheduling Calendar: **custom grid**

* **Decision:** Build the resource day view in-house rather than adopting a calendar library.
* **Why:** The core screen is one column per staff member with drag-to-book, buffer times and equipment conflicts — the product's differentiating surface, where full control over interaction is worth more than a library's defaults. FullCalendar's Resource Timeline sits behind **FullCalendar Premium at $480/developer/year**, recurring, with redistribution restrictions that are a poor fit for software shipped to on-premise customers. `react-big-calendar` (MIT) was the free alternative but would still have been fought at the edges.
* **Primitives used** — custom does not mean from scratch:
  * **CSS Grid** for layout (staff columns × time rows).
  * **dnd-kit** (MIT) for drag-to-create, drag-to-move and resize — pointer maths, touch support and keyboard accessibility are the tedious parts.
  * **date-fns-tz** for wall-clock ↔ UTC conversion against the business timezone.
  * Overlap layout (concurrent appointments in one slot) hand-written — a small interval-packing function whose behaviour should be controlled directly.

## 14. Account Security

### Authentication hardening

* **Password storage:** Argon2id. Minimum 12 characters, **no composition rules** — NIST SP 800-63B found that forced complexity produces predictable passwords.
* **Breach screening:** HaveIBeenPwned k-anonymity API (only a 5-character hash prefix leaves the server), falling back to a bundled common-password list for on-premise deployments without outbound internet.
* **Lockout — escalating and temporary, never permanent.** Progressive delay from the first failure; temporary lock after 10 failures at 15 minutes, then 30, then 60, capped at 24 hours for repeat offences. **Auto-unlocks on expiry**, with administrators able to unlock early. Permanent lock-until-admin was rejected: a known username would become a way to lock a clinic's front desk out mid-shift. Time-bounded escalation imposes the same attacker cost without handing out that weapon.
* **Owner notification** on lockout and on every password change.
* **Forced reset on cause, not on a timer.** A `must_change_password` flag is set for administrator-created accounts and after suspected compromise. Periodic expiry is **off by default** (NIST advises against rotation without evidence of compromise) but is exposed as a per-business setting for clients whose insurer or college demands it.
* **Reset flow:** single-use emailed token, 30–60 minute expiry, invalidating all active sessions on completion.
* **Session revocation:** password change, reset, or MFA reset revokes outstanding refresh tokens through a `jti` denylist in Redis. Without this, a stolen long-lived Staff token would survive the password change meant to kill it.

### Multi-factor authentication

* **TOTP (`pyotp`) is the default second factor. SMS MFA is never implemented** — it costs money per message and is weaker, being vulnerable to SIM-swap.
* **Per-business toggle, enabled by default for the Admin capability**, disableable in the admin panel so a solo operator is not forced into it.
* Prompted at initial login and once per 12 hours when entering Admin Mode — not on every mode switch, which would make the 15-minute admin window unusable.
* **Recovery codes** issued at enrolment; the primary lost-device path.
* **Email OTP** is available as the recovery fallback when recovery codes are also lost, and is selectable as a primary second factor per business for staff who will not install an authenticator app. It is **lower assurance** — NIST does not recognise email as an out-of-band authenticator channel, since the inbox typically lives in the same browser session as the attacker's foothold — and the admin panel states that tradeoff plainly rather than burying it. It also consumes the shared Resend daily quota.

### Rate limiting

* **Traefik rate-limit middleware** for blunt per-IP volumetric limits across all endpoints. Configuration, no application code.
* **Redis-backed per-account throttling on login.** IP-only limiting was rejected: distributed credential stuffing defeats it, and a clinic behind a single NAT would throttle itself.
* **Public booking endpoint — no CAPTCHA initially.** A honeypot field, per-IP and per-email daily booking caps, and automatic expiry of unconfirmed bookings. reCAPTCHA and Turnstile both ship visitor data to a third party, which cuts against the data-residency commitment being sold; spam bookings are visible to staff and cancellable, rather than silently damaging. Turnstile is added only if real abuse appears.
* **Form links** already carry high entropy; they need a rate limit and constant-time token comparison, nothing more.
* Failed login attempts are written to the audit log.

## 15. Concurrency Control

Two problems of the same class, each solved in the database rather than in application locking.

**Stock deduction** — a guarded single statement:

```sql
UPDATE products SET quantity = quantity - :qty
WHERE id = :id AND quantity >= :qty
```

Zero rows affected means insufficient stock. That statement *is* the concurrency control: no explicit row locks, no version column, and two simultaneous sales of the last item cannot both succeed. An append-only `stock_movements` row is written in the same transaction — not extra work, since the PRD already requires a stock adjustment log.

**Double-booking** — a Postgres exclusion constraint:

```sql
EXCLUDE USING gist (
  resource_id WITH =,
  tstzrange(starts_at, ends_at) WITH &&
) WHERE (status <> 'cancelled')
```

The database refuses overlapping bookings for the same staff member, room or equipment. This is stronger than application-level locking because correctness does not depend on every code path remembering to take a lock — a future endpoint that forgets receives a constraint violation instead of silently double-booking a treatment room. Buffer times are folded into the stored range.

## 16. Retention, Erasure and Audit

### Retention versus deletion requests

PIPEDA grants **no general right to erasure** — it requires destruction when data is *no longer necessary*, a different obligation. The CPPA that would have introduced a disposal right died with Bill C-27 in July 2026 with no successor enacted. Meanwhile Ontario's regulated health colleges require clinical records for **10 years from the last entry, or until 10 years after the client turns 18 — whichever is later**, so the retention date is a function of **date of birth**, not a fixed offset from the last visit.

* **Per-business retention profile** set at setup: `regulated_health` (retention obligation wins) or `general_business` (a salon or consultant has no such obligation, so deletion requests are honoured promptly).
* **Computed retention expiry stored per record** — `max(last_entry + 10y, dob + 18y + 10y)` for regulated profiles — so the purge job is a simple date scan.
* **A deletion request never deletes records under a live retention hold.** It creates an `erasure_request`, immediately purges what is not held (marketing preferences, contact details beyond the record, non-clinical notes), and suppresses the profile from active views. The client is told what is retained and why, which is the legally correct answer rather than a fudge.
* **Immutable does not mean forever.** Retaining data indefinitely is itself a PIPEDA violation, so a purge path must exist — but the application role has `DELETE` revoked on those tables. A **separate privileged purge role** runs the expiry job, deletes expired rows, and records the erasure in the audit log (the fact, never the data). This is the only path permitted to delete from immutable tables.
* **Crypto-shredding:** a per-customer document key means purging can destroy the key rather than rewriting partitioned tables.

Retention periods are configuration, not constants — each client confirms them with their own college or counsel.

### Access auditing

PHIPA requires health information custodians to produce an electronic audit log of **who viewed** a record, not merely who modified it. Mutation logging alone is therefore insufficient, and read logs are also what make a breach tractable: without them there is no way to answer which records were actually exposed.

* The audit log records **PHI access events** — viewing a customer profile, session note or form PDF — with actor, timestamp and record.
* This is the highest-volume table in the system and is **yearly-partitioned from day one**, like appointments.
* An admin report answers **"who accessed this client's record"**, satisfying both the PHIPA audit duty and a client's own access request.

### Breach notification

PIPEDA requires reporting to the Privacy Commissioner and affected individuals on "real risk of significant harm", plus records of **all** breaches retained 24 months. These are obligations on the *business*, not functions the software must implement: the breach register and notification timelines live in the runbook with a template, rather than as a half-built incident module nobody maintains. Individual notification uses the existing notification engine.

## 17. First-Run Bootstrap: **token-gated setup wizard**

* A browser setup wizard collects **three things and no more**: the business name, its IANA timezone, and the first administrator account (email + Argon2id password, 12-character minimum). Branding is configured later from the admin panel, and TOTP enrolment is prompted at that administrator's first login once MFA exists — neither belongs in the flow that claims the instance, and both are reachable only after there is an account to attach them to. The timezone is collected here rather than later because appointment storage semantics depend on it and retrofitting wall-clock handling is painful.
* **The wizard is gated by a setup token** minted on first boot, written to stdout (`docker compose logs app`) and to a `0600` file on disk. Without this, a setup wizard on a public IP is a race — whoever reaches the instance first between deployment and setup owns the clinic. An attacker who finds the URL first meets a token prompt they cannot answer.
* **Only the token's SHA-256 digest is persisted** (single-row `setup_token`, inserted `ON CONFLICT DO NOTHING` so concurrent workers settle the race in the database). The plaintext lives in the log and the file and nowhere else. A restart therefore does not invalidate a token the operator has already copied out: later boots re-log the file path instead of minting. Re-minting happens only when the file has gone missing, since a token nobody can read cannot finish setup.
* On completion the token row and its file are destroyed in the same transaction that writes `businesses.setup_completed_at`, and that flag makes `POST /api/setup` return 404 for good — across restarts and for processes that never saw the token. `GET /api/setup/status` keeps answering (the frontend needs to know whether to show the wizard) and `GET /api/setup/timezones` stays available for editing the timezone later.

## 18. Form Template Versioning

A form signed in 2027 must render exactly as signed when retrieved in 2034, while admins continue editing templates.

* **`form_templates` (stable identity) + `form_template_versions` (immutable, numbered).** Submissions reference a *version*, never the live template. Answers are JSONB keyed by field id; the signed PDF remains the legal artifact.
* **Stable field keys.** Every field carries a UUID that survives rewording across versions. Without it, "Do you have diabetes?" becoming "Any diabetes diagnosis?" silently breaks historical comparison and prefill.
* **Versions freeze on publish.** Editing creates a draft that becomes a new version on publish. No in-place edits to a published version, whether or not it has submissions — a simpler rule than "frozen after first use", and it removes a class of race.
* **Secure links pin the version at issue time.** A link sent Monday and submitted Wednesday renders Monday's form even if the template was republished Tuesday; otherwise the client signs something other than what staff reviewed, which undermines the consent itself.
* **Compliance checks run against template identity, not version** — "has a signed waiver of type X within 12 months" must not fail because wording changed. Publishing offers a **`requires_resignature`** flag so an admin can force re-signing when a new version adds a materially new clause. That is an editorial judgement, so it is an explicit choice rather than an inferred diff.
* **Offline/tablet submissions carry their pinned version id**, which makes the offline conflict case tractable: a form completed offline is valid against the version it was opened with.

## 19. Availability Computation

* **Computed in Python, not SQL.** Inputs load via indexed queries — staff hours, time off, existing appointments, room and equipment bookings — and are intersected in memory. The rules are editorial and will keep changing (buffers, lead times, holidays, per-service resource needs); a testable pure function over interval lists stays debuggable where recursive SQL does not. Volumes are trivial: one business, a few staff, a day or week at a time.
* **Order of operations:**
  1. Generate staff availability in **business-local wall-clock**, then convert to UTC instants — never the reverse. Spring-forward days have no 02:00–03:00 and fall-back days repeat 01:00–02:00; generating locally and localizing afterwards makes those days correct without special cases.
  2. Subtract time off, holidays, and existing appointments inflated by their buffers.
  3. Intersect with required room and equipment free intervals for the service.
  4. Slide `duration + buffers` across remaining free intervals at a **fixed granularity, default 15 minutes, per-business setting**. Continuous start times were rejected: they produce 09:07 slots and fragment the day into unbookable slivers.
* **"Any available provider"** is the union of per-staff availability, with the staff member assigned at booking time.
* **Caching:** Redis key per `(business, service, date)`, ~60 second TTL, plus explicit invalidation on booking creation/cancellation, schedule edits, time off, and equipment changes. The TTL is the safety net for a missed invalidation.
* **Staleness is safe by design.** Displayed slots are advisory; the database constraints in §15 are authoritative. A client who loses the race receives "that time was just taken" rather than a double-booked room.
* **Bounded horizon:** at most the configured booking window (default 90 days), capping the cost of any single request.
* **Group bookings** (multi-service visits) require sequential slot search across appointments — the main source of complexity in this algorithm.

## 20. Scheduling Concurrency Policy

The §15 exclusion constraint assumed a booking occupies staff and room continuously. Salon workflows break that assumption: during a colour processing interval the stylist is free while the chair is not.

* **Spaces and equipment keep the hard `EXCLUDE` constraint, always.** A chair or a device cannot be in two places; this is physics, not policy.
* **Staff overlap is policy**, governed by `max_concurrent_appointments_per_staff` (default 1) and enforced by a **trigger**, because exclusion constraints cannot read configuration while triggers can. Still database-enforced, not application-enforced.
* A numeric limit rather than a boolean expresses the real rule — "this stylist can run two chairs but not three."
* Availability computation reads the same setting: staff busy intervals stop being subtracted once concurrency allows.
* This delivers the colour-processing case **without modelling service segments at all** — the stylist is simply bookable twice.

## 21. Money, Tax and Financial Documents

### Representation

* **Integer cents** throughout. Tax computed per line, rounded half-up to the cent, then summed — consistent and documented, since CRA permits per-line or per-invoice rounding provided the two are not mixed.

### Tax model

Verified rates as of 2026: ON 13% HST; NS 14% (reduced from 15% in April 2025); NB/NL/PEI 15%; BC and MB 12% (GST + PST/RST); SK 11%; QC 14.975% (GST + QST, shown as separate lines per Revenu Québec); AB and territories 5% GST. Massage therapy remains **taxable** — Bill C-323 would have exempted it and has not passed — while physiotherapy and chiropractic are exempt under Schedule V. Small suppliers under $30,000 are unregistered and charge nothing.

* **Tax rates are data with effective dates**, not constants: `(jurisdiction, component, rate, effective_from, effective_to)`. Nova Scotia moved recently and the massage exemption is live legislation; hardcoded rates guarantee an emergency code change.
* **The business address determines the available component set** (ON → HST; BC → GST + PST; QC → GST + QST; AB → GST), pre-filled from province and admin-confirmable — the same pattern as the timezone selector.
* **Each service and product enables components individually.** A per-item `standard | exempt` category was rejected because it cannot express the common real case: in BC a massage is GST-taxable but PST-exempt, while a retail oil sold beside it is taxable for both.
* **Default to enabled; the admin disables for exempt items.** Over-charging is correctable with a credit note; under-charging means the business pays CRA out of pocket years later on revenue it never collected. The safer default is the one that fails recoverably.
* **Invoices snapshot resolved components, rates and amounts at issue** and are never recomputed.
* **Place of supply is assumed to be the business location**, correct for in-person services and point-of-sale retail. Shipping product to another province would follow the customer's province and needs a second input — out of scope, recorded so it is not discovered as a bug.
* **Liability boundary:** the product never determines whether a given practitioner's service is exempt. It ships defaults and per-item toggles; the business and their accountant own the determination, as with retention periods.

### Document types — two, not three

* **Invoice** (voidable, cancel-and-reissue, gapless per-business numbering for CRA) with **payment records** attached. A "payment receipt" is a *render* of the invoice showing what has been paid — no separate table or lifecycle.
* **Treatment receipt** is a genuinely separate document because its content is not a subset of the invoice: practitioner name and registration number, date of service, and the per-session attributable value. A package's treatment receipt shows $80 on a date when no payment occurred at all.
* Payment records are append-only; refunds create reversing records rather than editing history, consistent with stock movements and package credits.

### Insurance

* **Per-session treatment receipts are generated on appointment completion regardless of payment method**, so the claimable document always exists. Canadian extended health insurers reimburse per **date of service**, not date of payment — a prepaid ten-pack cannot be claimed as one lump in January.
* **Staff carry licence number and designation** (e.g. "RMT #12345"). Insurers reject receipts lacking them, so these are validation rules on receipt generation, not optional profile fields.
* **No direct-billing integration.** Electronic submission runs through TELUS Health eClaims and equivalents, which require provider enrolment and partner certification; practitioners already use those portals directly.
* **Direct billing is a tracked manual workflow** — a small accounts receivable with two payers per invoice: `total`, `insurer_expected`, `insurer_actual`, `client_paid`, and `outstanding = total − insurer_actual − client_paid`. States: `submitted` → `adjudicated` (paid / partial / denied) → `settled`; a denial is simply `insurer_actual = 0`.
* This exists because adjudication typically lands after the client has left. A clinic estimates $100 coverage, collects $100 at the desk, the insurer pays $80 against an exhausted annual maximum, and $20 is owed by a client who is no longer in the building. Outstanding balances surface on the client profile and a front-desk list.

### Packages and bundles

* **Package** = one service × N, prepaid. **Bundle** = a basket of several services at one price. Both issue credits redeemed against completed appointments.
* **Services and retail are billed separately** — no combined invoices, and no mixed service/product packages. This supersedes the original PRD wording and has the side benefit that a single invoice never mixes exempt and taxable lines across categories.
* Mechanics mirror inventory: a guarded counter update plus append-only `package_credit_movements`. One concurrency model to understand, audit trail included.
* **Credits deduct on appointment completion, not on booking.** Booking-time deduction requires reversal on every cancellation and no-show, and reversals are where balances drift.
* **Eligibility is explicit** — a package names the services its credits may be spent on.
* **Price snapshot at purchase** fixes the per-session value, used for refunds, treatment receipts and the liability report.
* **Refund on partial use** = paid − (sessions used × *regular* price). Without the regular-price clause, a client uses nine of ten discounted sessions, refunds, and keeps the package rate on a partial pack.
* **Expiry configurable, default off.** Runbook flag: prepaid consumer purchases are regulated in several provinces (Ontario bans gift-card expiry outright) and prepaid service packages sit near that line — each business confirms with counsel rather than inheriting a default.
* **Non-transferable by default**; admin override writes an audit entry.
* **Outstanding liability report** — unredeemed credits × snapshot value — because a business holding $40,000 of undelivered sessions should be able to see that number.

### Commission reporting

Reporting only. **No payroll, no payouts, no tax withholding, no pay periods** — the owner reads the number and pays people however they already do.

* **Two rates per staff member**: `commission_rate_services` and `commission_rate_retail`. Salons pay a lower cut on product than on services. Per-individual-service rates were rejected as a matrix nobody maintains.
* **Attribution:** service revenue is unambiguous because appointments are per-staff; retail needs a `sold_by` field on the invoice.
* **Basis:** pre-tax, post-discount — commission on revenue earned, not on tax collected for the government.
* **Packages earn on delivery, not on sale**, at the per-session attributable value. Otherwise the seller banks a whole pack's commission and the people doing the work earn nothing.
* **The rate is snapshotted on the invoice line at issue.** Without it, raising a rate in June silently rewrites every earlier month's report.
* Refunds and voids post reversing entries in the period they occur.
* Output is one report — date range, per staff, service and retail revenue and commission — with CSV export.

### Out of scope

* **Tips and gratuity** — handled outside the system.
* **Memberships and recurring billing** — requires a payment gateway, which the PRD deliberately excludes.
* **Booth/chair rent** — collected outside the system.
* **Retail product bundles** — "3 oils for $50" is a discount, not a bundle. A service package exists because payment and delivery are separated in time, which is what makes credits necessary; retail has no such gap. Invoice discounts (already required, since commission is calculated post-discount) cover this case and every other promotion. A named gift set is a product with its own SKU and stock, not a bundle of other products.

## 22. Availability Overrides and Walk-ins

### The override principle

**Availability rules are advisory — a human may override with the action logged. Physical resources are absolute — never overridable.**

* **Shift end, time off, vacation** → warning plus explicit confirmation ("this runs 30 minutes past Maria's shift end — continue?"), recording who authorized it. A client arriving at 16:30 for an hour-long service, with a therapist willing to finish at 17:30, is a normal business decision, not an error state.
* **Room, equipment and chair conflicts** → hard block. The conflict is physical, so no override exists.
* Applies to **booked appointments as well as walk-ins** — a client phoning at 16:30 to request 17:00 is the same situation, and special-casing walk-ins would mean two code paths for one rule.
* Gated by an RBAC capability `override_availability`: staff may override their own schedule, administrators may override anyone's. The reason is labour rather than security — front desk unilaterally committing a barber's evening is a different act from the barber choosing to stay.
* **Staff-side only.** The public booking portal never offers slots outside shift hours; clients cannot self-serve into someone's evening.

This extends the physics-versus-policy split already applied to concurrency in §20.

### Two different things called "walk-in"

* **"Can you fit me in?"** — the clinic case. The client wants an appointment, just soon. Served by a **"next available" search shortcut** in the booking flow, always enabled, requiring no new entity and no configuration.
* **"Take a number"** — the barbershop case. No appointment time exists; service begins when a chair frees. This requires a queue, enabled per business via `enable_walk_in_queue`, **default off**. When off, the queue screen does not exist for that vertical.

### Queue model

`queue_entries`: customer (or bare name), requested service, optional preferred staff, arrival time, status `waiting | in_service | done | abandoned`.

* **A queue entry converts into an appointment when service starts.** A waiting client has no time range, and every scheduling mechanism specified here — the exclusion constraint, availability computation, buffers — assumes one exists. Modelling a walk-in as an appointment starting "now" would require rewriting its start time continuously as the queue shifts, with each rewrite fighting the constraint. Once genuinely in service it *is* an appointment, so treatment receipts, package redemption, commission attribution and session notes work unchanged.
* `abandoned` ("left without being seen") has no appointment equivalent and is a real metric for a walk-in business.
* **Booked appointments always win.** A queue entry may start only if the staff member is free now, within shift (subject to the override above), and free for the full service duration before their next booked appointment. Without this, a busy walk-in morning silently consumes the afternoon's booked clients.
* **Identity:** quick-create with name only, phone optional — a barbershop cannot collect a full profile at the door. Records upgrade to full customers on return. Accepted cost: some duplicates, pending the deferred merge flow.
* **Essential-form checks run at queue entry, not at service start.** The waiting room is the only window in which a client can actually complete a waiver on a tablet; checking at service start discovers the gap when they are already in the chair.
* **Wait estimate** is computed as remaining service durations ahead divided by available staff, and labelled an estimate. Crude, but the alternative is staff guessing aloud and being wrong.
* **Notifications are on-screen only.** "You're next" by SMS is the obvious want, but SMS is opt-in and tenant-paid, and email is useless to someone in a waiting room. A trigger becomes available if a tenant enables SMS; nothing is built specifically for it.
* **Self check-in kiosk is out of scope for v1** — a separate surface with its own abuse and privacy questions. Staff-added entries cover the workflow.
