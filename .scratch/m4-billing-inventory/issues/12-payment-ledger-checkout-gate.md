# 12 — Manual payment ledger + checkout gate

**What to build:** Recording manual payments against an issued invoice — cash, e-transfer,
external card, or an insurer, each with amount, method, reference and collector, supporting
split payments. Checkout requires the client's own portion fully settled before it can
complete, unless an admin/owner explicitly authorizes an outstanding balance with a reason. An
insurer's externally-approved-but-unpaid amount is recorded as pending, never as received.
(#54 stories 34-42.)

**Blocked by:** 11 (an issued invoice to pay against)

**Status:** ready-for-agent

- [ ] Staff can record one or more payment entries against an invoice: amount, payer
      (client/insurer), method, reference, collector — split payments supported
- [ ] Outstanding balance is derived from the payment ledger plus any prepaid allocation, never
      stored as an independently editable boolean/flag
- [ ] Checkout refuses to complete while the client's own portion is unsettled, unless an
      admin/owner authorizes the exception with a recorded reason
- [ ] An externally-approved insurer amount shows as pending until an actual payment entry
      records the money arriving — approval alone never marks it received
- [ ] Ordinary outstanding balances (pending insurer money, an authorized client exception)
      are visible on the client's history and a billing list for manual follow-up
