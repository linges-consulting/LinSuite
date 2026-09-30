"""The business record and the two audit logs: the tables that belong to no single domain.

Single-tenant means exactly one business, so `id` is pinned to 1 by a CHECK constraint —
the database, not the application, refuses a second business.

The profile and brand colours live on this row because every domain reads them — a receipt
needs the address and the currency symbol, the shell needs the colours. The two *images* do
not: they are in `settings/models.py`, off this row, so a megabyte of PNG is not loaded by
every request that only wanted the business name.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

# The codes migration 0008's CHECK names, spelled out rather than imported: `core/` imports
# no domain module, and `settings.timezones.PROVINCES` is where the screen reads them from.
PROVINCE_CODES = ("AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT")


class Business(Base):
    __tablename__ = "businesses"
    # Every rule the database actually holds, declared here too: a CHECK that exists only in
    # a migration is one the next `alembic revision --autogenerate` proposes dropping.
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_businesses_single_row"),
        CheckConstraint(
            "province IS NULL OR province IN (" + ", ".join(f"'{p}'" for p in PROVINCE_CODES) + ")",
            name="ck_businesses_province",
        ),
        # Canada Post's format, in the one shape the API normalises to: `A1A 1A1`, upper case.
        CheckConstraint(
            "postal_code IS NULL OR postal_code ~ '^[A-Z][0-9][A-Z] [0-9][A-Z][0-9]$'",
            name="ck_businesses_postal_code",
        ),
        CheckConstraint(
            "brand_primary ~ '^#[0-9a-f]{6}$' AND brand_secondary ~ '^#[0-9a-f]{6}$'",
            name="ck_businesses_brand_hex",
        ),
        CheckConstraint(
            "slot_granularity_minutes BETWEEN 5 AND 60 AND slot_granularity_minutes % 5 = 0",
            name="ck_businesses_slot_granularity",
        ),
        CheckConstraint(
            "booking_horizon_days BETWEEN 1 AND 365", name="ck_businesses_booking_horizon"
        ),
        CheckConstraint(
            "vip_visit_threshold BETWEEN 2 AND 1000", name="ck_businesses_vip_visit_threshold"
        ),
        CheckConstraint(
            "retention_profile IN ('regulated_health', 'general_business')",
            name="ck_businesses_retention_profile",
        ),
        CheckConstraint(
            "email_sender IS NULL OR email_sender IN ('resend', 'smtp', 'mailgun')",
            name="ck_businesses_email_sender",
        ),
        CheckConstraint(
            "mailgun_region IS NULL OR mailgun_region IN ('us', 'eu')",
            name="ck_businesses_mailgun_region",
        ),
        CheckConstraint(
            "cancellation_cutoff_hours >= 0", name="ck_businesses_cancellation_cutoff_hours"
        ),
        CheckConstraint(
            "booking_daily_cap_per_ip >= 1", name="ck_businesses_booking_daily_cap_per_ip"
        ),
        CheckConstraint(
            "booking_daily_cap_per_email >= 1", name="ck_businesses_booking_daily_cap_per_email"
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, server_default="1", autoincrement=False
    )
    name: Mapped[str] = mapped_column(String(200))
    # IANA name. Recurring availability is wall-clock against this; see CLAUDE.md "Time".
    timezone: Mapped[str] = mapped_column(String(64))
    # Set once, when the setup wizard completes. Non-null permanently disables setup.
    setup_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Periodic password expiry. Null — the default — is off, which is the NIST position:
    # rotation without evidence of compromise degrades password quality. Exposed only
    # because some insurers and colleges require it (PRD §1, tech-stack §14).
    password_rotation_days: Mapped[int | None] = mapped_column(Integer)
    # On by default (PRD §1, tech-stack §14): an account that can administer the business is
    # the one worth a second factor. Disableable, because a solo operator with one device is
    # otherwise one lost phone away from being locked out of their own business — which is
    # why this is a setting rather than a rule.
    mfa_required_for_admin: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Email as a *primary* second factor, off by default. It is lower assurance — NIST does
    # not recognise email as an out-of-band channel, since the inbox usually lives in the
    # same browser session an attacker already has — so a business opts into it knowingly.
    # It is available as the recovery fallback regardless; that is `auth/mfa.py`'s business.
    mfa_email_otp_allowed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    # --- the profile (PRD §1, §7; written by `settings/routes.py`) -------------------------
    # The address is what the tax components will be derived from, so it is structured rather
    # than one free-text block: `province` is the field a later rate table joins on.
    address_line1: Mapped[str | None] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    province: Mapped[str | None] = mapped_column(String(2))
    postal_code: Mapped[str | None] = mapped_column(String(7))
    # Fixed for now. A column rather than a constant because an address without a country is
    # not an address, and the day a second one is supported this is where it is read from.
    country: Mapped[str] = mapped_column(String(2), server_default=text("'CA'"))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(320))
    # Labelled free text, not a validated registration number. The formats differ per
    # jurisdiction and change; a business that mistypes its own GST number notices on the
    # first invoice, and a regex that refuses a valid one is unfixable from the screen.
    gst_hst_number: Mapped[str | None] = mapped_column(String(64))
    pst_qst_number: Mapped[str | None] = mapped_column(String(64))
    currency_symbol: Mapped[str] = mapped_column(String(8), server_default=text("'$'"))
    receipt_footer: Mapped[str | None] = mapped_column(Text)
    # Six-digit hex, lowercase. The CHECK is in the migration: these end up as CSS variable
    # values on `<html>`, so "is it really a colour" is worth asserting in two places.
    brand_primary: Mapped[str] = mapped_column(String(7), server_default=text("'#1d4ed8'"))
    brand_secondary: Mapped[str] = mapped_column(String(7), server_default=text("'#0f766e'"))

    # --- what the availability engine reads (tech-stack §19; `scheduling/availability.py`) --
    # The step slot starts are offered on, from local midnight. Five-minute steps, at most an
    # hour; the CHECK is in migration 0013.
    slot_granularity_minutes: Mapped[int] = mapped_column(Integer, server_default="15")
    # How far ahead anything is computed. A year at most: a horizon caps the cost of a request.
    booking_horizon_days: Mapped[int] = mapped_column(Integer, server_default="90")
    # Classification (pre-flight D8): a client is "vip" once they reach this many `completed`
    # appointments. Never stored on the customer — computed at read time from the count, so
    # lowering this number reclassifies everybody on the next read with no write to `customers`.
    vip_visit_threshold: Mapped[int] = mapped_column(Integer, server_default="10")

    # --- retention (ADR-0001, pre-flight D3; `customers/retention.py`) ---------------------
    # Which obligation wins. Default retain-side: a salon left on `regulated_health` holds data
    # a little longer, a clinic left on `general_business` would purge a chart it must keep —
    # only the first mistake is recoverable. Switched on Settings → Security, never the wizard.
    retention_profile: Mapped[str] = mapped_column(
        String(32), server_default=text("'regulated_health'")
    )
    # When an administrator last saved the profile explicitly. NULL means nobody has chosen
    # yet, and the Security panel asks them to (the default is a default, not a decision).
    retention_profile_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # --- notification email sender (Phase 12 Task 2, #11; `notifications/providers.py`,
    # `notifications/credentials.py`) --------------------------------------------------------
    # Which provider a real send uses. NULL means unconfigured — Task 6's settings panel shows
    # a banner until one is chosen and a test send succeeds. The two `*_encrypted` columns are
    # direct AES-256-GCM field encryption under `NOTIFICATION_CREDENTIAL_KEY`
    # (`notifications/credentials.py`), the same shape `mfa_encryption_key` already uses for
    # TOTP secrets — one business row, no per-record wrapped-key scheme.
    email_sender: Mapped[str | None] = mapped_column(String(16))
    resend_api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    resend_from_address: Mapped[str | None] = mapped_column(String(320))
    # Set only by a successful Resend test send (Task 6, not built here). NULL gates Task 5's
    # trigger functions from ever attempting a real Resend send — "email features remain
    # disabled behind a banner until a test send succeeds" (#11 acceptance criterion).
    resend_domain_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    smtp_host: Mapped[str | None] = mapped_column(String(255))
    smtp_port: Mapped[int | None] = mapped_column(Integer)
    smtp_username: Mapped[str | None] = mapped_column(String(255))
    smtp_password_encrypted: Mapped[str | None] = mapped_column(Text)
    smtp_from_address: Mapped[str | None] = mapped_column(String(320))
    # SMTP's equivalent of `resend_domain_verified_at`: SMTP has no domain to verify, only a
    # test send that succeeded, so it is named for what it actually records.
    smtp_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- notification email sender: Mailgun (M7, #115; `notifications/providers.py`) --------
    # A third `email_sender` choice, over Mailgun's HTTP messages API — the reason it exists at
    # all is that DigitalOcean blocks outbound SMTP on new droplets, so an HTTP-based sender is
    # the one that actually works there. Same encryption/verification shape as Resend above:
    # `mailgun_api_key_encrypted` under `NOTIFICATION_CREDENTIAL_KEY`, `mailgun_verified_at` set
    # only by a successful test send. `mailgun_region` picks the API host: Mailgun's EU accounts
    # only work against the EU host, never the US one.
    mailgun_api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    mailgun_domain: Mapped[str | None] = mapped_column(String(255))
    mailgun_region: Mapped[str | None] = mapped_column(String(2))
    mailgun_from_address: Mapped[str | None] = mapped_column(String(320))
    mailgun_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- notification SMS sender (Phase 12 Task 3, #11; `notifications/providers.py`) -------
    # Off by default: unlike email (which degrades to `console`), SMS is opt-in per tenant —
    # tech-stack §6 treats it as an adapter nobody gets until they ask and supply credentials.
    # `twilio_auth_token_encrypted` is direct AES-256-GCM field encryption under the same
    # `NOTIFICATION_CREDENTIAL_KEY` the Resend/SMTP columns above use — one key, reused, not a
    # second one (still one business row, no crypto-shred requirement).
    sms_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    twilio_account_sid: Mapped[str | None] = mapped_column(String(64))
    twilio_auth_token_encrypted: Mapped[str | None] = mapped_column(Text)
    twilio_from_number: Mapped[str | None] = mapped_column(String(32))

    # --- reminder scheduler (Phase 12 Task 5, #11; `notifications/reminders.py`) ------------
    # Hours-before-appointment offsets a reminder fires at, reckoned against this business's
    # own local wall clock (see that module's docstring for why a raw instant-minus-`timedelta`
    # is wrong across a DST boundary). JSONB, like `AuditEvent.event_metadata` and
    # `Appointment.overridden_rules` — a plain list of small integers doesn't need a real
    # array type, and this keeps the same column shape the rest of the app already uses for
    # "a list, stored". Task 6's settings panel is what will let an administrator edit this;
    # until then every deployment gets the same default, a day before and two hours before.
    reminder_intervals_hours: Mapped[list[int]] = mapped_column(
        JSONB, server_default=text("'[24, 2]'::jsonb")
    )

    # --- booking-portal policy (Phase 6 Tasks 3-4, #10; `scheduling/public.py`,
    # `settings/notifications_routes.py`) ----------------------------------------------------
    # All four read at the API — never only hidden behind a UI toggle (#10's own acceptance
    # criterion: "disabling online cancellation removes the capability at the API"). Editable
    # through the same notification/portal settings panel (Task 4).
    online_cancellation_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Hours before `starts_at` a client may still cancel or reschedule online, reckoned
    # against the plain instant (unlike `reminder_intervals_hours`, this is not a wall-clock
    # recurrence rule — "24 hours before this exact appointment" means the same thing across
    # a DST boundary that a weekly shift pattern would not).
    cancellation_cutoff_hours: Mapped[int] = mapped_column(Integer, server_default=text("24"))
    # The whole-portal switch (Task 4): `scheduling/public.py`'s availability and booking
    # routes both 404 the same way an unknown/not-`bookable_online` service already does, once
    # this is off — distinct from `bookable_online`, which is per-service. Defaulted on so an
    # upgrade through this migration changes nothing until an administrator turns it off.
    online_booking_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Admin-configurable counterparts of what were `scheduling/public.py`'s fixed
    # `_DAILY_CAP_PER_IP`/`_DAILY_CAP_PER_EMAIL` constants (Task 2) — same defaults, now a
    # setting rather than a number only a code change could move.
    booking_daily_cap_per_ip: Mapped[int] = mapped_column(Integer, server_default=text("20"))
    booking_daily_cap_per_email: Mapped[int] = mapped_column(Integer, server_default=text("5"))

    # --- walk-in queue toggle (Phase 7 Task 1, #12; `scheduling/models.py::QueueEntry`) ------
    # Off by default. CLAUDE.md's own distinction: "fit me in" (an always-on "next available"
    # search shortcut in the ordinary booking flow, Phase 7 Task 3) is not this. This is "take
    # a number" — a business opts in because service there starts when a chair frees, not by
    # appointment time. With it off, no queue surface exists anywhere in the product (#12's own
    # acceptance criterion) — Tasks 2/8 gate the CRUD routes, the nav entry and the frontend
    # route on this same column; this task only makes the toggle itself exist and be honest.
    enable_walk_in_queue: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    # --- bill review authority toggles (#64, M4 spec #54 stories 15-17) --------------------
    # Both default *on* — the opposite of `enable_walk_in_queue` above, because these two gate
    # an override path on a screen (#63's bill review) that already exists for everybody, not
    # a whole surface nobody has opted into yet. Off means "no *new* use of the staff-facing
    # convenience path"; it never touches an admin/owner's own ordinary access to a bill from
    # their own screen in Admin Mode, and it never erases request history already made
    # (`billing/bill_authority.py`'s own docstring has the enforcement detail).
    enable_bill_override_requests: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true")
    )
    enable_inline_admin_bill_edit: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true")
    )

    # --- low-stock alert opt-in (#62; `inventory/stock.py`, `notifications/triggers.py::
    # notify_low_stock`) --------------------------------------------------------------------
    # Off by default, following `enable_walk_in_queue`'s exact shape (0040): the in-app warning
    # (`inventory/routes.py`'s `is_low_stock`) is always shown regardless of this flag — this
    # only gates the *email* side. No new capability: reading/writing it goes through the
    # existing `settings/notifications_routes.py` panel, behind that router's own
    # `Requires("admin")` — the same reasoning `enable_walk_in_queue`'s own migration gives.
    low_stock_alert_email_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )

    # --- CTI demo mode (Phase 14, #16; `scheduling/cti.py`) --------------------------------
    # Off by default, `enable_walk_in_queue`'s exact shape (0040): with it off, the "simulate
    # incoming call" control is unreachable anywhere in the product — `scheduling/cti.py`'s
    # simulate endpoint 404s, exactly the way a disabled queue's endpoints do — so a working
    # clinic never sees a button that exists only to demo a phone system it doesn't have.
    # Phone lookup itself (the real feature CLAUDE.md's CTI note promises) needs none of this;
    # it is always on, gated only by `customers.view`.
    demo_mode: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class AuditEvent(Base):
    """Append-only: who did what, and when (ADR-0002).

    `linsuite_app` has INSERT and SELECT and nothing else — UPDATE and DELETE are revoked
    *and* a trigger raises on both. Two mechanisms because they fail differently: a grant
    is undone by one careless `GRANT ALL`, and a trigger is undone by one `DISABLE
    TRIGGER`. Erasure under the retention policy is the purge role's job, never this one.

    Identifiers only. The content of whatever was viewed or changed never comes in here;
    copying it would duplicate personal health information into a second table with a
    different retention horizon.
    """

    __tablename__ = "audit_events"
    # The two reads this table exists for: one actor's trail, and a window of time.
    __table_args__ = (
        Index("ix_audit_events_actor", "actor_user_id", "occurred_at"),
        Index("ix_audit_events_occurred_at", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Deliberately not a foreign key. An audit trail outlives the accounts it describes:
    # a FK would either block the retention purge or, with ON DELETE SET NULL, make
    # deleting a user an UPDATE of this table — which the append-only trigger refuses.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    event_type: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(64))
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AccessLogEntry(Base):
    """One PHI read: who opened whose record, as what, from where (ADR-0002 §1).

    A second table rather than more columns on `AuditEvent`, because it answers a different
    question — "who *looked*" — at a different volume, and is yearly-partitioned for it.
    `postgresql_partition_by` here is what keeps autogenerate from proposing to recreate the
    table flat; migration 0019 is where the partitions, the sequence and the trigger live.
    The partition key has to be in the primary key, hence `(id, occurred_at)`.

    Written only by `core.access_log.LogAccess`. Identifiers only, never what was seen.
    """

    __tablename__ = "audit_access_log"
    __table_args__ = (
        PrimaryKeyConstraint("id", "occurred_at"),
        Index("ix_audit_access_log_customer", "customer_id", "occurred_at"),
        Index("ix_audit_access_log_actor", "actor_user_id", "occurred_at"),
        {"postgresql_partition_by": "RANGE (occurred_at)"},
    )

    # An explicit sequence rather than `Identity()`: Postgres 16 refuses an identity column
    # on a partitioned table.
    id: Mapped[int] = mapped_column(
        BigInteger, server_default=text("nextval('audit_access_log_id_seq')")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # No foreign keys, as on `AuditEvent`: the trail outlives what it describes.
    actor_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # The role's name at the moment of access — roles rename, the log must not.
    actor_role: Mapped[str] = mapped_column(String(64))
    customer_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16))
    ip: Mapped[str | None] = mapped_column(INET)


class Document(Base):
    """One sealed document — a signed consent, an intake form, a scan, or (#55/ADR-0003) a
    financial document sealed under the business's own key (`core/documents.py`).

    Immutable (CLAUDE.md): `linsuite_app` holds SELECT and INSERT only, and `documents_guard`
    (0026) refuses UPDATE and DELETE to everyone but the table owner — except DELETE by
    `linsuite_purge` for a client not under a retention hold. Written and read only through
    `store_document`/`fetch_document`.

    **Two key tiers, picked by `key_owner`, never inferred.** `'customer'` (default, the
    original tier, unchanged): `customer_id` is required and references the client's *key
    row*, not `customers`, with no cascade (ADR-0001 rule 7) — inserting a document locks the
    key row it is sealed under, so a purge deleting that key serialises against the insert,
    and the purge must delete the documents first. A string FK, so `core/` still imports no
    domain module. `'business'`: sealed under the deployment's one `business_document_keys`
    row (`billing/keys.py`) instead, `customer_id` always NULL — that FK is exactly what must
    not exist for a financial document, since it must outlive any one customer's crypto-shred
    (a paid invoice is a CRA record for six years regardless of what happens to that client's
    chart). `linked_customer_id` is a business-keyed document's own, plain reference to
    `customers.id` for the client it is for (NULL for an anonymous retail sale) — it carries
    no locking behaviour and survives that client's shred untouched, which is the whole
    reason it is a separate column from `customer_id` rather than the same one doing double
    duty. `retain_until` is that document's own CRA-clock expiry, tracked independently of
    `customers.retention_expires_at`; nothing purges by it yet (a later ticket's job) — this
    one only makes sure the value has somewhere to live from the first business document on.
    """

    __tablename__ = "documents"
    __table_args__ = (
        # A re-run render of the same source is `ON CONFLICT DO NOTHING`, never a second copy.
        UniqueConstraint("kind", "source_id", name="uq_documents_kind_source_id"),
        CheckConstraint("octet_length(digest) = 32", name="ck_documents_digest_length"),
        CheckConstraint("key_owner IN ('customer', 'business')", name="ck_documents_key_owner"),
        CheckConstraint(
            "(key_owner = 'customer' AND customer_id IS NOT NULL) OR "
            "(key_owner = 'business' AND customer_id IS NULL)",
            name="ck_documents_owner_customer_id",
        ),
        CheckConstraint(
            "key_owner = 'business' OR linked_customer_id IS NULL",
            name="ck_documents_owner_linked_customer_id",
        ),
        Index("ix_documents_customer_id", "customer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("customer_document_keys.customer_id")
    )
    # 'customer' (default) or 'business' — see the class docstring. Text, not a DB enum: the
    # two-value CHECK above is the same enforcement a `citext`/native enum would give, without
    # an `ALTER TYPE` the day a third tier ever exists.
    key_owner: Mapped[str] = mapped_column(Text, server_default=text("'customer'"))
    linked_customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))
    # CRA six-year rule (#55): set by a business-keyed document's caller, NULL on a
    # customer-keyed row (that tier's retention is the client's own hold, not this column).
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # What produced it (`form_submission`, ...) and that thing's id.
    kind: Mapped[str] = mapped_column(Text)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    content_type: Mapped[str] = mapped_column(Text)
    # `core.crypto.seal`: nonce || ciphertext || tag.
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    # HMAC-SHA256 of the plaintext under a subkey of the sealing key (`core/documents.py`),
    # checked after every decrypt. Keyed, not a bare hash: it dies with the key.
    digest: Mapped[bytes] = mapped_column(LargeBinary)
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReportExport(Base):
    """One queued CSV export of any report kind (#86, M5 spec #82) — generalises what was
    `commission_exports` (R29) so `access_log` and `package_liability` (#87/#88) share the
    same table, the same Celery task and the same 7-day lifetime instead of each growing its
    own.

    A domain registers a builder for its `kind` at import time (`core/exports.py`), which is
    also what dispatches the shared Celery task and what an endpoint calls to insert a row
    and its `report.export_requested` audit event together. Nothing here validates `kind`
    against a fixed list: the acceptance criterion for adding one is "a builder plus its
    registration," and a CHECK on `kind` would mean a migration on top of that for every new
    kind — `core/exports.py::is_registered` is where an unrecognised kind is refused instead,
    before any row is written.

    `params` is that kind's own filter shape (a date range and staff id for `commission`; a
    customer id and date range for `access_log`; whatever `package_liability` needs) —
    opaque here, read only by the registered builder. `expires_at = created_at + 7 days`
    (ADR-0001's export-lifetime amendment): a download past it is a 410, and the nightly
    `core.tasks.cleanup_expired_exports` deletes the row. `linsuite_app` may DELETE this
    table under the baseline (0001) default grant, unlike an immutable table — an export is
    a working copy, never a record under retention.
    """

    __tablename__ = "report_exports"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'ready', 'failed')", name="ck_report_exports_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now() + interval '7 days'")
    )
