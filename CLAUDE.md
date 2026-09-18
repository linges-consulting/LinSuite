# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo state

Directory skeleton only — no package manifests, no dependencies installed, no actual code yet. Git repo initialized, remote `origin` → `github.com/linges-consulting/LinSuite` (empty, nothing pushed).

```
app/
  backend/src/{auth,customers,scheduling,inventory,forms,billing,notifications,core}/
  backend/tests/
  frontend/src/
  frontend/tests/
infra/            # docker-compose, traefik/nginx config — not yet written
docs/             # app_prd.md, tech-stack.md, agents/
scripts/          # empty
```

`core/` under `backend/src` is the only non-domain module — shared config, DB session, security/JWT helpers, and `store_document`/`fetch_document`.

Note: the PRD's section 8 tree shows these domains directly under `app/`; the scaffold nests them under `app/backend/src/` to leave room for `app/frontend/`. The domain boundaries are what matter.

There are no build/lint/test commands yet because no manifest or tooling exists. Once `package.json`/`pyproject.toml` are added, update this file with real commands (build, lint, single-test invocation) — don't invent them before then.

## Stack

Full rationale in `docs/tech-stack.md` — each decision records why, and what would justify revisiting it.

- **Backend:** Python, FastAPI, SQLAlchemy 2.0 async + Alembic, PostgreSQL, Redis, Celery, WeasyPrint/ReportLab
- **Auth:** self-rolled JWT (FastAPI DI + passlib) — no IdP, no `fastapi-users`; the dual-mode session policy is custom
- **Frontend:** React + Vite + Tailwind + shadcn/ui (Radix), TanStack Table, TanStack Query for server state, Context for auth/mode. No Redux/Zustand
- **Calendar:** custom grid — CSS Grid + dnd-kit + date-fns-tz + hand-written overlap packing. No calendar library (FullCalendar Premium rejected: $480/dev/yr + redistribution restrictions for on-prem)
- **Notifications:** Resend (email, enabled); Twilio SMS admin-configurable and off by default, tenant supplies own credentials
- **Testing:** pytest + pytest-asyncio + httpx (backend), Vitest + React Testing Library (frontend)
- **Infra:** Docker Compose (`app`, `db`, `redis`, `worker`, `traefik`) on DigitalOcean *or* customer on-prem; manual runbook provisioning, no IaC
- **Backups:** nightly `pg_dump` → restic → configurable target, offsite with Object Lock
- **Observability:** structured JSON logs to stdout only — no Sentry yet
- **CI:** GitHub Actions on PR — ruff, pytest, vitest, compose build. No CD

## What this product is

Universal white-label, single-tenant business & practice management app (booking, customers, inventory, forms, notifications, billing/receipts, session notes). Full spec: `docs/app_prd.md`.

Constraints any implementation must honor:

- **Modular monolith**, one deployable app, strict domain boundaries: `auth`, `customers`, `scheduling`, `inventory`, `forms`, `billing`, `notifications`. Decoupled enough to extract later, but shipped as one app.
- **Single-tenant**: each business gets its own VM/server + database stack. No shared multi-tenant database — never design cross-tenant data access. On-prem is a supported target, so nothing may depend on a cloud-provider-specific service.
- **Celery workers** for notification delivery (exponential backoff retry), PDF generation, and exports. These must not run inline in request handlers.
- **JWT sliding-window sessions**, two policies: Admin Mode short (15–30 min) timeout requiring re-auth; Staff Mode long-lived. Dual-role users explicitly switch modes — never a merged permission set.
- **RBAC is capability-scoped**, not role-name based.
- **Secure form links**: short-lived, single-use, high-entropy (UUIDv4/HMAC-signed, 24–48h expiry).
- **Resource locking**: shared equipment/spaces must use atomic DB locking to prevent double-booking.

### Document storage and immutability

- PDFs live in **PostgreSQL `bytea`**, AES-256-GCM encrypted in the app layer, SHA-256 verified on read. No object storage, no filesystem. Access goes through `store_document`/`fetch_document` in `core/`.
- Three document classes, and the distinction matters: **immutable** (signed consents, waivers, intake forms — `UPDATE`/`DELETE` revoked and trigger-blocked), **voidable** (invoices — never edited in place; cancel sets status + links a replacement, original retained for CRA's 6-year rule), **lockable** (session notes — mutable until locked).
- Immutability is enforced by DB grants and triggers, never by PDF permission flags, which are advisory and trivially stripped.
- Audit log is an append-only table with the same enforcement.

### Time

- Appointment instants: `timestamptz`. Recurring availability rules (weekly matrix, split shifts, cancellation windows): **local wall-clock times** against the business's IANA timezone. Storing recurrence as UTC instants shifts every schedule by an hour at DST boundaries.

### Concurrency — enforce in the DB, not in app locks

- **Stock:** `UPDATE ... WHERE id = :id AND quantity >= :qty`, check rowcount. The statement is the lock.
- **Spaces/equipment:** `EXCLUDE USING gist (resource_id WITH =, tstzrange(...) WITH &&)` — always hard. A chair can't be in two places.
- **Staff overlap is policy, not physics:** `max_concurrent_appointments_per_staff` (default 1), enforced by trigger since constraints can't read config. Set to 2 for salon colour-processing workflows. This is why service segments aren't modelled.
- **Same principle for overrides:** availability rules (shift end, time off, vacation) are *advisory* — overridable by a human, logged, gated by `override_availability`. Physical resources (room, equipment, chair) are *absolute* — never overridable. Staff-side only; the public portal never offers out-of-shift slots.

### Domain rules that aren't obvious from the schema

- **One appointment = one service + one staff member.** Multi-service visits are linked appointments sharing a `booking_group_id`.
- **Two things are called "walk-in".** "Fit me in" = a next-available search shortcut in the normal booking flow, always on, no new entity. "Take a number" = the opt-in `queue_entries` table (`enable_walk_in_queue`, default off), which **converts to an appointment when service starts** — a waiting client has no time range, and every scheduling mechanism here assumes one. Booked appointments always win.
- **Services and retail invoice separately.** Never combined; no mixed service/product packages.
- **Two document types:** invoice (+ attached payment records; a "payment receipt" is just a render) and **treatment receipt** (date of service, practitioner licence number, per-session value). They're separate because payment and delivery happen on different dates — a prepaid 10-pack is paid once but generates ten claimable receipts, and insurers reimburse per date of service.
- **Package credits deduct on completion, not booking** — booking-time deduction needs reversals, and reversals drift.
- **Commission earns on delivery, not sale**, and rates are snapshotted on invoice lines so rate changes don't rewrite history. Reporting only — no payroll.
- **Form submissions reference an immutable template *version*, never the live template.** Secure links pin the version at issue. Field keys are UUIDs stable across rewording.
- **Tax components come from the business address; each catalog item toggles them individually.** A `standard|exempt` enum can't express "GST yes, PST no", which is the normal BC case. Rates are effective-dated data; invoices snapshot them.
- **Money is integer cents.** Tax per line, half-up, then summed.
- Out of scope: tips, memberships/recurring billing, booth rent, direct-billing integration.

### Retention, erasure, audit

- **Immutable ≠ forever.** Indefinite retention violates PIPEDA. A *privileged purge role* — never the app role — runs the expiry job and is the only path allowed to delete from immutable tables.
- Retention expiry is `max(last_entry + 10y, dob + 18y + 10y)` for `regulated_health` businesses — it depends on **date of birth**, not last visit. `general_business` tenants honour deletion requests promptly.
- A deletion request never removes records under a live retention hold; it purges what isn't held and suppresses the profile.
- **Audit log records reads, not just writes.** Viewing a profile, note, or form PDF is an auditable event (PHIPA requires it, and breach scoping is impossible without it). Highest-volume table — partitioned from day one.

### Auth specifics

- Argon2id, 12-char min, no composition rules, breach-screened. Lockout is **escalating and temporary with auto-unlock** — never permanent (permanent lockout is a DoS vector against a known username).
- TOTP only for MFA; **SMS MFA is never implemented**. Email OTP exists as a documented lower-assurance fallback.
- Password/MFA changes revoke refresh tokens via a `jti` denylist in Redis.
- First run: token-gated setup wizard, token to stdout + `0600` file, self-disables after completion.

### Out of scope for v1 — don't build these

- i18n (English only) · live VoIP webhook/WebSocket transport (phone lookup and screen-pop UI ship; a demo-mode toggle drives simulated events) · cold-storage archive tiering, replaced by yearly table partitioning · Sentry/error tracking · CD pipeline.
- There is **no WebSocket layer in v1**. Deferring CTI is what keeps it out — don't reintroduce it casually.

## Agent skills

### Issue tracker

GitHub Issues (`linges-consulting/LinSuite`), via `gh` CLI. PRs-as-request-surface: off. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five canonical labels, unchanged (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `CONTEXT.md` + `docs/adr/`. See `docs/agents/domain.md`.
