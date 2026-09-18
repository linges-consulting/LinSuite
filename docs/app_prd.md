
# Product Requirements Document (PRD): Universal White-Label Business & Practice Manager

> **v1 scope note.** Implementation decisions live in `docs/tech-stack.md`. Deferred from v1: French/i18n support (English only), the VoIP CTI webhook integration (the screen-pop UI and phone lookup ship; only the live telephony transport is deferred), and SMS delivery (present as an admin-configurable channel, disabled by default). Removed entirely: cold-storage archive tiering, replaced by database partitioning.

## 1. White-Label Branding, Multi-Tenant Settings & Role-Based Access

* **Business Branding Engine:** Admin settings allowing businesses to upload a custom company logo, favicon, and select primary/secondary brand color schemes (HEX/RGB) so the client portal, booking widgets, and invoices reflect their unique visual identity.
* **Dynamic Business Profile:** Configurable fields for business name, local tax registration numbers (e.g., GST/HST/VAT), currency symbols, and custom receipt footers.
* **User Accounts & Dual-Role Capability:** Secure login supporting users who hold both staff/provider and administrator capabilities.
* **Multi-Staff Support:** Ability to handle solo operations or scale to multiple team members sharing a facility with color-coded schedules.
* **Staff Scheduling & Partial Availability Management:**
* Configurable weekly matrix for staff working hours supporting split shifts or partial availability blocks (e.g., working 9:00 AM – 12:00 PM, unavailable/lunch break 12:00 PM – 3:00 PM, and working 3:00 PM – 6:00 PM).
* Time-off, vacation, and statutory holiday blockouts that override standard availability and prevent booking collisions.
* **Time Representation:** Appointment instants are stored as `timestamptz` (UTC). Recurring availability rules — the weekly matrix, split shifts, cancellation windows — are stored as **local wall-clock times** against the business timezone, never as UTC instants. "Works Mondays 9:00 AM–12:00 PM" is a wall-clock rule that must stay at 9:00 AM local across daylight-saving transitions; storing it as a UTC instant would silently shift every staff member's schedule by an hour twice a year.


* **Resource, Space, & Equipment Management:**
* **Spaces / Rooms / Stations:** Allocation tracking to assign appointments to specific physical spaces (e.g., treatment rooms, styling chairs, or consultation booths).
* **Shared Equipment Mapping:** Management of scarce or mobile assets (e.g., specialized machines, devices, or tools) that can be linked or mapped to specific services to prevent resource conflicts. Conflict prevention is enforced by a database exclusion constraint over (resource, time range) rather than application-level locking, so a code path that forgets to lock receives a constraint violation instead of silently double-booking a room or device. Buffer times are included in the stored range.


* **Capability-Scoped Access Control (RBAC):** Granular permission management controlled by administrators to define custom roles and toggle functional capabilities.
* **Explicit Admin/Staff Context Switching & Session Policy:**
* **Context Switcher:** Users with dual privileges explicitly switch their active operational view between **Admin Mode** and **Staff/Provider Mode**.
* **Sliding-Window Session Expiration (JWT-Powered):**
* Switching to **Admin Mode** triggers a strict short session timeout policy (15–30 minutes) requiring re-authentication. However, switching back and forth within an active 15-minute window maintains continuity without forcing a hard login wall, provided the hard session limit hasn't expired.
* **Staff Mode** utilizes longer token expiration windows to prevent workflow interruptions during active client delivery.


* **Account Security Baseline:**
* **Multi-factor authentication (TOTP):** enabled by default for the Admin capability, toggleable per business. Prompted at initial login and once per 12 hours on entering Admin Mode. Recovery codes issued at enrolment; email OTP available as a lower-assurance fallback or as a primary second factor for staff who will not use an authenticator app. SMS-based MFA is never offered — it carries per-message cost and is weaker than TOTP.
* **Brute-force protection:** progressive delay from the first failed attempt, escalating temporary lockout (15 / 30 / 60 minutes, capped at 24 hours) that **auto-unlocks**, with early unlock available to administrators. Permanent lockout is deliberately not implemented, as it would let an attacker lock a business out of its own front desk using only a known username.
* **Password policy:** Argon2id hashing, 12-character minimum, no composition rules, screened against known-breached passwords. Forced reset occurs on evidence of compromise, not on a schedule; periodic rotation is available as a per-business setting for clients whose insurer or college requires it.
* **First-run setup** is performed through a browser wizard gated by a one-time token written to the container logs, which collects the first administrator, business name, timezone and branding, then disables itself permanently.





## 2. Customer Profile, Service Catalog, Inventory & Flexible Forms Management

* **Customer Database & Segmentation:**
* Centralized storage for customer contact details, birth dates, and emergency or secondary contact information.
* **Retention Profile & Deletion Requests:** Each business is configured as either `regulated_health` (statutory retention obligations override deletion requests) or `general_business` (no such obligation, so deletion requests are honoured promptly). For regulated profiles, each record carries a **computed retention expiry** of `max(last entry + 10 years, date of birth + 18 years + 10 years)` — Ontario colleges require records until ten years after a minor client reaches adulthood, so expiry depends on birth date rather than a fixed offset from the last visit.
* A deletion request never removes records under a live retention hold. It immediately purges everything not held (marketing preferences, contact details beyond the record, non-clinical notes), suppresses the profile from active views, and tells the client what is retained and why. Held records are purged automatically once their retention expiry passes.
* **Customer Classification Tags:** Automated or admin-defined status flags (e.g., *New Customer*, *Repeat Customer*, *VIP/Valued Customer*) calculated based on visit history and lifetime appointment counts.


* **Flexible Service & Catalog Customization:**
* Admins can define staff categories/roles and build a custom service catalog (e.g., treatments, consultations, classes, or salon services) with custom durations, buffer times, pricing, and required space/equipment dependencies.
* **Per-Item Tax Configuration:** The business address determines which tax components are available (Ontario → HST; British Columbia → GST + PST; Quebec → GST + QST, shown as separate lines; Alberta → GST only). Each service and product then enables components individually, because real cases require it — in British Columbia a massage is GST-taxable but PST-exempt, while a retail product sold beside it is taxable for both. Components default to enabled and are disabled for exempt items, since over-charging is correctable by credit note while under-charging is paid out of pocket at audit. The product ships defaults and toggles; the business and its accountant own the exemption determination.


* **Inventory Management & Retail Sales:**
* **Product Catalog & Variants:** Tracking for physical retail items with SKUs, barcodes, pricing, and variant options.
* **Real-Time Stock Tracking:** Automatic live inventory deduction when retail items are sold alongside services or standalone. Deduction is performed by a guarded single-statement update that cannot oversell under concurrent sales, with an append-only stock movement row written in the same transaction to serve the adjustment log.
* **Low-Stock Alerts:** Automated alerts triggered when stock drops below customizable thresholds, with role-restricted inventory adjustment permissions via RBAC.


* **Configurable Form Builder & Staff Types:**
* Admins can configure custom intake questionnaires, waivers, or intake documents mapped to specific staff types or service offerings.
* Ability to mark specific forms as **Essential** (e.g., annual liability waivers or updated health/intake histories).
* **Versioned Templates:** Templates hold a stable identity; every published edit creates a new immutable version, and submissions reference the version they were signed against so a form retrieved years later renders exactly as signed. Secure links pin the version at the moment of issue, so a client never signs a revision that staff did not review. Essential-form compliance is evaluated against template identity rather than version, with an optional **requires re-signature** flag at publish time for revisions that add materially new clauses.


* **Multi-Channel Submission Workflows & Non-Guessable Secure Links:**
* **Short-Lived, Non-Guessable Digital Link Workflow:** Staff generate a unique, cryptographically secure URL token containing high-entropy random identifiers (e.g., UUIDv4 or HMAC-signed tokens) that cannot be brute-forced or guessed. Links expire automatically after a set window (e.g., 24 to 48 hours) or immediately after single-use submission. Sent via email/SMS to the customer for pre-arrival completion.
* **In-Clinic Tablet Mode:** Business mode allowing staff to pull up a form on an in-clinic device (like an iPad) and hand it directly to the customer for on-site completion and digital signature. Includes local browser caching (IndexedDB/LocalStorage) to prevent data loss during network hiccups, alongside a traditional paper form fallback option.
* **Print & Scan Alternative:** Option to print a blank form, obtain a manual physical signature, and scan/upload the completed document.


* **Immutable & Encrypted Form Archival:**
* Upon submission or scan upload, completed forms are compiled into PDF documents, encrypted with AES-256-GCM in the application layer, and stored in PostgreSQL as `bytea` alongside a SHA-256 hash verified on every read.
* **Immutability is enforced by the database, not by the PDF.** PDF permission flags are advisory and trivially stripped. Signed consents, waivers and intake forms live in tables where `UPDATE` and `DELETE` are revoked from the application role and blocked by trigger.
* Scanned uploads are downsampled (≈150 DPI grayscale) before storage to keep the 10-year retention footprint manageable.


* **Compliance & Validation Flagging:**
* Automated background checks scanning customer profiles against required essential forms, triggering visual dashboard reminders and profile alerts for missing or expired records.



## 3. Scheduling, Telephony & Appointment Booking

* **Online Booking Portal & Admin Cancellation Controls:**
* A clean, user-friendly widget for business websites where customers view available service times, book slots, and handle cancellations or rescheduling.
* **Delivery:** a hosted booking page at `/book/<business-slug>`, embeddable in the business's own website via an iframe snippet (`?embed=1` hides page chrome; frame height is auto-sized via `postMessage`). An iframe is used rather than an injected JavaScript widget so that the host site's CSS cannot break the booking flow and the booking page cannot read the host page's DOM. Businesses wanting only a "Book Now" link use the hosted page directly with no embed code.
* **Admin-Controlled Client Portal Policies:** Administrators can globally toggle client-facing online booking and cancellation permissions. If online cancellation is disabled by admin policy, clients must call the business directly to cancel or modify appointments.
* **Cancellation Window Enforcements:** Configurable time buffers restricting self-service cancellations too close to appointment times (e.g., no cancellations within 24 hours).


* **Appointment Tracking:** Clear status tags for appointments (e.g., Confirmed, Completed, Cancelled, No-Show).
* **One Appointment Per Provider:** Each appointment holds a single service and a single staff member, with its own resources, status and treatment receipt. A visit combining several services with different providers creates linked appointments sharing a booking group, so the client sees one booking and cancellation or rescheduling can act on the whole group.
* **Configurable Staff Concurrency:** Administrators set the maximum concurrent appointments per staff member (default 1). Raising it to 2 supports salon workflows where a stylist starts a second client during a colour processing interval. Spaces and equipment are never permitted to overlap regardless of this setting, since a chair or device cannot be in two places at once.
* **Availability Overrides — Advisory Versus Absolute:** Shift end, time off and vacation are *advisory*: staff may book past them after an explicit confirmation, with the override and its authorizer recorded. A client arriving at 4:30 PM for an hour-long service with a willing provider is a normal business decision, not an error. Room, equipment and chair conflicts are *absolute* and can never be overridden, because the conflict is physical. Overrides are governed by an RBAC capability — staff may override their own schedule, administrators may override anyone's — and are staff-side only: the public booking portal never offers slots outside scheduled hours.
* **Walk-In Handling (Two Distinct Workflows):**
* **"Can you fit me in?"** — the clinic case, where the client wants an appointment soon. Handled by a **next-available search shortcut** in the booking flow, available to every business with no configuration.
* **"Take a number"** — the barbershop case, where no appointment time exists and service begins when a chair frees. Handled by an opt-in **walk-in queue** (`enable_walk_in_queue`, default off) tracking arrival time, requested service, optional preferred provider, and status including *abandoned* for clients who leave before being seen. A queue entry becomes an ordinary appointment the moment service starts. Booked appointments always take precedence: a walk-in may only begin if the provider is free for the full service duration before their next booking. Essential-form requirements are surfaced at queue entry, while the client is still in the waiting room and able to complete a waiver. Wait times are shown as estimates; client notification is on-screen, and self check-in kiosks are out of scope for v1.
* **Incoming Call Customer Lookup (CTI):**
* **Ships in v1 — phone number lookup.** Staff enter a caller's number and get the customer profile, classification status, previous service provider history, and quick-scheduling shortcuts. Useful with no telephony integration at all; it is simply search.
* **Ships in v1 — screen-pop panel with simulated trigger.** The pop-up panel is fully built and driven by an incoming-call event source. A "simulate incoming call" action, exposed behind a **demo mode toggle in admin settings (off by default)**, drives that event source for demonstrations without shipping a confusing button into a working clinic's toolbar.
* **Deferred — live VoIP webhook transport.** The webhook receiver and WebSocket push are not built in v1. This is the only feature requiring real-time server push, so deferring it keeps WebSocket connection management, authentication and reconnect handling out of the v1 architecture entirely. When a client with a specific VoIP provider appears, only the event *source* changes; the panel and lookup already exist.



## 4. Notification & Automated Communication Engine

* **Multi-Channel Delivery Architecture:**
* **Email — tenant-owned sender.** Because mail must come from the *business's own* domain, each business supplies either its own Resend account with a verified domain (the setup wizard displays the required DNS records and polls verification) or the SMTP credentials of an existing Microsoft 365 or Google Workspace account, which needs no DNS work because the domain is already authenticated. Sender reputation therefore stays isolated per business — one clinic's complaint rate cannot degrade another's deliverability. Email features remain disabled behind a banner until a test send succeeds, rather than silently delivering into spam folders. Amazon SES (Canada Central region) is the documented upgrade path at higher volume.
* **SMS — admin-configurable, disabled by default.** No free SMS tier exists from any provider. A business that wants SMS supplies its own Twilio credentials in the admin panel and bears the per-message cost. The notification engine treats SMS as an adapter that remains unimplemented until a tenant requests it.


* **Trigger-Based Communication Rules:**
* **Booking Confirmations:** Instantly fired upon creation of an appointment via portal or manual entry.
* **Configurable Time-Based Reminders:** Automated advance reminders sent at admin-defined intervals (e.g., 24 hours and 2 hours prior) to minimize client no-shows.
* **Modifications & Cancellations:** Automated notices sent when a session time is adjusted or canceled.
* **Form & Intake Delivery:** Direct dispatch of secure, non-guessable form links to client communication channels.
* **Bundle & Package Notices:** Alerts tracking multi-session pack balances and renewal triggers.


* **Asynchronous Resiliency & Retry Workers:**
* All communications are queued asynchronously via Redis background workers to block UI freezing. Includes automated exponential backoff retries for failed API requests and permanent delivery failure logs surfaced directly on client profiles for front-desk visibility.



## 5. Session Notes & Documentation (Flexible Mode)

* **Custom Note Templates:** Configurable note-taking modules supporting structured frameworks (such as SOAP notes for wellness providers, or general project/session logs for consultants and coaches).
* **Graphical / Visual Markup Charts:** Interactive diagrams (such as anatomical body maps for health practices, or floor/layout/custom diagrams for other industries) allowing staff to drop color-coded pins, shade zones, and add timestamped text annotations.
* **Note Security & Immutability:** Options to lock historical session notes to maintain professional record integrity.

## 6. Invoicing, Retail & Financials (Manual Record-Keeping)

* **Separate Billing for Services and Retail:** Service sales and retail product sales are invoiced separately; they are never combined on one document, and mixed service-plus-product packages are not offered.
* **Professional Receipts:** Automatic generation of clean invoices carrying custom business tax numbers, with tax components itemized separately.
* **Two Document Types:**
* **Invoice** — what is owed. Payment records attach to it, and a payment receipt is a render of the invoice showing what has been paid. Invoices use gapless per-business numbering for CRA audit purposes.
* **Treatment Receipt** — what was delivered. Generated on appointment completion regardless of payment method, showing date of service, service description, the practitioner's name and registration number, and the value attributable to that session. This is the document clients submit to insurers.
* The two are separate because payment and service delivery routinely occur on different dates: a prepaid ten-session package is paid once in January but delivered across the year, and Canadian extended health insurers reimburse per **date of service**, never per date of payment.


* **Practitioner Credentials:** Staff records carry licence number and designation (e.g. "RMT #12345"). Insurers reject receipts lacking them, so these are validated at receipt generation rather than treated as optional profile fields.
* **Insurance Direct Billing — Tracked, Not Integrated:** No electronic submission integration (TELUS Health eClaims and equivalents require provider enrolment and partner certification; practitioners use those portals directly). The product instead tracks the resulting money: expected insurer amount, actual adjudicated amount, client payment, and the outstanding balance. Adjudication usually lands after the client has left — a clinic estimates $100 of coverage and collects $100 at the desk, the insurer pays $80 against an exhausted annual maximum, and $20 remains owed by someone no longer in the building. Outstanding balances surface on the client profile and a front-desk list.
* **Invoice Lifecycle — Voidable, Not Immutable:** Unlike signed consents, invoices are not permanently locked. An invoice is never edited in place; cancelling it sets a `cancelled` status and links to its replacement document. The cancelled invoice is **retained, not deleted** — CRA requires business records for six years, and an unexplained gap in invoice numbering is worse to an auditor than a visible voided document.
* **Manual Payment Tracking:** Ability to manually log how appointments and retail purchases were settled (e.g., Cash, e-Transfer, External Card Reader, or Direct Corporate/Insurance Billing) without an integrated payment gateway.
* **Package & Bundle Tracking:** A **package** is a prepaid quantity of one service ("10 massages for $800"); a **bundle** is a basket of several services at one price ("assessment plus two follow-ups, $200"). Both issue credits redeemed against completed appointments — never at booking, since booking-time deduction requires reversal on every cancellation and no-show. Credits name the services they may be spent on, carry a per-session value snapshotted at purchase, are non-transferable unless an administrator overrides with an audit entry, and may optionally expire (off by default — prepaid consumer purchases are regulated in several provinces, so each business confirms this with counsel). Refunds on partial use return the amount paid less sessions already used valued at the regular price. An outstanding liability report shows unredeemed credits at snapshot value.
* **Commission Reporting:** Each staff member carries a service commission rate and a retail commission rate, used to produce a per-staff revenue and commission breakdown over a date range, exportable as CSV. Commission is calculated pre-tax and post-discount; package sessions earn commission on delivery rather than on sale, so the practitioner performing the work is credited rather than whoever sold the package. Rates are snapshotted on invoice lines at issue so that changing a rate does not rewrite historical reports. This is a reporting aid for the owner only — the product performs **no payroll, payouts, withholding or pay-period processing**.
* **Out of scope:** tips and gratuity (handled outside the system), memberships and recurring billing (would require the payment gateway this product deliberately excludes), booth or chair rent arrangements, and retail product bundles — a "3 for $50" offer is an invoice discount rather than a credit-bearing bundle, since retail delivery is immediate and nothing needs tracking over time.

## 7. Comprehensive Admin Configuration Suite

Admins have full, centralized control over the operational parameters of the software without code changes via dedicated management panels:

* **Branding & Localization Panel:** Upload logos/favicons, configure HEX color themes, currency symbols, tax rates (GST/HST/VAT), and business metadata. Includes the business **IANA timezone** selector — required before the business can go live, pre-filled as a suggestion from the entered province but confirmed by a human, since Canadian provinces do not map cleanly onto timezones (Saskatchewan skips DST, BC's Kootenays are Mountain, northwestern Ontario is Central, Labrador spans two zones). Interface language is English only in v1.
* **RBAC & User Management Panel:** Create custom security roles, assign granular permission toggles, manage staff schedules, split-shift availability windows, and internal rate tracking. Also governs the MFA policy (TOTP on/off, email-OTP allowance), optional periodic password rotation, early unlock of locked-out accounts, and the business retention profile.
* **Service Catalog & Resource Matrix Panel:** Build services, set durations, buffer times, and map required spaces/rooms and shared equipment dependencies. Also configures per-item tax components, packages and bundles, and the maximum concurrent appointments per staff member.
* **Intake Form & Essential Rules Panel:** Design custom forms, add signature fields, and flag specific questionnaires as mandatory for compliance.
* **Telephony, Portal Policy & Notification Panel:** Toggle client-facing booking/cancellation window rules, customize email/SMS notification templates, configure reminder timing intervals, supply Twilio credentials to enable the optional SMS channel, and switch on **demo mode** for simulated CTI screen-pops. VoIP webhook endpoint management arrives with the deferred live telephony transport.
* **Inventory & Stock Alert Panel:** Configure minimum stock thresholds, monitor product variants, and review stock adjustment logs.

## 8. Infrastructure, Modular Directory Structure, Security & Archival

* **Single-Tenant Deployment Model:** Each business operates its own dedicated virtual machine instance and database container stack, entirely avoiding data bleed. DigitalOcean (Canadian regions) is the standard target; **customer-hosted on-premise servers are a first-class deployment option** running the identical Compose stack, differing only in volume paths and TLS certificate issuance.
* **Provisioning:** New tenants are provisioned by following a written manual runbook. No infrastructure-as-code tooling — per-business stack configuration varies enough by use case that a parameterised module would accumulate branches faster than it saves effort at current tenant counts.
* **Modular Monolith Directory Architecture:**
* Built as a cohesive single deployable application structured around strict domain boundaries to keep local development simple while organizing modules cleanly for potential future microservice extraction:


```text
app/
├── core/                         # Shared infrastructure, config, database base classes
├── auth/                         # Authentication, RBAC & context-switching tokens
├── customers/                    # Customer profiles, segmentation & GDPR/PIPEDA compliance
├── scheduling/                   # Appointments, working hours, space/equipment locks & CTI
├── inventory/                    # Retail products, variants, stock tracking & low alerts
├── forms/                        # Intake questionnaires, secure digital links & PDF archival
├── billing/                      # Invoicing, manual payments & session packages
├── notifications/                # Email/SMS templates, Redis queues & exponential backoff
├── main.py                       # Application entry point wiring all module routers
└── Dockerfile                    # Single container build for dedicated VM deployment

```


* **Retention Performance — Yearly Partitioning:**
* Heavy tables (appointments, documents, audit log) use native PostgreSQL declarative partitioning by year, so date-filtered operational queries touch only the current partition and never scan a decade of history.
* This **replaces the previously specified cold-storage tiering**, and removes the archive background worker from scope. Partitioning meets the same goal — keeping active queries fast — without a second storage system that must be backed up and restored in lockstep with the database. If a deployment's database does become unwieldy, an old partition is detached and dumped to an encrypted file as a runbook step, not by a service.


* **Performance Isolation & Background Processing:**
* Heavy operations—such as compliance reporting, data exports, and PDF generation—are isolated and offloaded to asynchronous Celery worker queues (via Redis) to prevent blocking core live scheduling and charting.


* **Tamper-Proof Audit Logging:**
* System-level tracking for role activities and record modifications written to an append-only PostgreSQL table with `UPDATE` and `DELETE` revoked from the application role and blocked by trigger, ensuring unalterable compliance histories.
* **Access (read) auditing:** viewing a customer profile, session note or archived form is itself recorded, with actor, timestamp and record. PHIPA requires custodians to produce an electronic log of who *viewed* a record, not only who changed it — and without read logs there is no way to determine which records were exposed during a breach. This is the highest-volume table in the system and is yearly-partitioned from the outset.
* **"Who accessed this client's record" report** in the admin panel, serving both the statutory audit duty and client access requests.
* **Purge authority:** immutability does not mean indefinite retention, which would itself breach PIPEDA. A separate privileged database role — never the application role — runs the retention expiry job, deletes expired records, and writes the erasure to the audit log (the fact of erasure, never the erased data). Documents are crypto-shredded by destroying their per-customer key.
* **Breach notification** obligations (reporting to the Privacy Commissioner, notifying affected individuals, and retaining a register of all breaches for 24 months) rest with the operating business and are covered by the deployment runbook and a notification template rather than by a dedicated software module.


* **Reverse Proxy & Caching Layer:**
* **Reverse Proxy (e.g., Nginx or Traefik):** Single entry point handling SSL termination, routing, and security headers.
* **Caching Layer (e.g., Redis):** Positioned to cache static assets, public booking availability slots, and session states, minimizing primary database load.


* **Data Residency & Hosting:** Deployment targeted to Canadian cloud data center regions to comply with local privacy and data residency expectations.
* **Long-Term Record Archival & Retention:** Built-in data retention policies ensuring operational and session records are securely retained for required compliance windows (e.g., minimum 10 years).
* **Encryption Standards:**
* **In-Transit:** Mandatory HTTPS/TLS 1.3 encryption for all traffic and service-to-service communication.
* **At-Rest:** Consent and intake PDFs are encrypted with AES-256-GCM in the application layer before being written to the database, so they remain ciphertext in the database and in every backup. Underneath that, DigitalOcean Volumes provide LUKS AES-256 encryption automatically; on-premise deployments are responsible for their own full-disk encryption, and where it is absent the application-layer encryption is the only protection.
* **Key Escrow (critical):** The PDF encryption key is escrowed **separately from database backups**, offline. A restored database without its key is ten years of permanently unreadable ciphertext — a backup that appears healthy and is worthless. Escrow is an explicit runbook step at install and on rotation.


* **Disaster Recovery & Backups:**
* Nightly `pg_dump` (which includes document `bytea`) pushed off-box by **restic**, encrypted client-side. The destination is configuration, not code — `BACKUP_TARGET=b2|s3|sftp|local` — so on-premise customers can target a local NAS plus an offsite copy while hosted tenants target Backblaze B2 directly. A 24-hour RPO is accepted; continuous WAL archiving is the documented upgrade for tenants who cannot lose a day.
* **Ransomware resilience — the tenant server cannot destroy its own backups.** The backup credential is scoped to write and read with **no delete permission**; the offsite bucket enforces **Object Lock with a 30–90 day retention window**, so snapshots survive even fully compromised server credentials; and pruning runs monthly from an operator workstation using a privileged key that never exists on a tenant machine.
* **Silent failure detection:** a healthcheck ping follows every backup run, with an alert if no successful backup has completed in 36 hours.