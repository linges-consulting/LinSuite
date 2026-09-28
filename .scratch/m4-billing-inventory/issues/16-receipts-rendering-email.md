# 16 — Treatment receipts + invoice rendering, print/email

**What to build:** Per-appointment treatment receipts, released only after bill review and
checkout complete (never at completion itself), retaining service date, provider identity,
final attributable value and correct payment status (prepaid value shown as such, pending
insurer amounts never shown as paid). Both treatment receipts and invoice/payment-receipt
documents render as PDF, are printable/downloadable, and can be emailed as an attachment
through M3's verified sender — which requires extending `NotificationProvider` to carry
attachment bytes, since it doesn't today. Uses #01's business-owned document storage. (#54
stories 87-93; "Treatment receipt timing" and "PDF email attachments" implementation-decision
bullets.)

**Blocked by:** 11 (checkout must be reachable), 01 (business-owned document storage)

**Status:** ready-for-agent

- [ ] A treatment receipt is released per appointment only after its bill has been reviewed and
      checkout completed — never immediately at appointment completion
- [ ] Receipt content: business identity, document identity, client identity where applicable,
      service date, service/provider, final charge or prepaid attributable value, tax
      breakdown, payment status (prepaid shown as prepaid, pending insurer shown as pending,
      never as paid)
- [ ] Clinical treatment receipts include the practitioner's designation/licence snapshot;
      non-clinical businesses are never forced to carry clinical fields
- [ ] `NotificationProvider` (and all three adapters — Console/Resend/Smtp) gain attachment
      support without breaking any existing non-attachment caller
- [ ] Staff can print/download without a verified sender configured; email requires the
      verified sender and is an explicit staff action, never automatic
- [ ] A rendering or delivery failure never undoes the underlying checkout/commission/stock
      effects that already committed; a resend never repeats the financial action
- [ ] Opening a client-linked financial document is an audited read event, following the
      existing `LogAccess` pattern
