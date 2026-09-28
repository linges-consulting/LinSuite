# M4 progress

> Note (#71's own agent): `.superpowers/sdd/m4.md` and this progress file did not exist
> anywhere in this repository or worktree when this ticket started (`git log --all` shows no
> commit ever added anything under `.superpowers/`, and the directory was empty on disk). This
> agent could not read the `## #60 detail` / `## #65 detail` sections the ticket brief
> referenced and instead read `billing/models.py`, `billing/packages.py`, `billing/invoices.py`,
> `billing/invoice_numbering.py` and `billing/allocation.py` directly. This file now exists with
> only the `## #71 detail` section below — earlier tickets' own detail sections were never
> recovered and are not fabricated here.

| Ticket | Status | Commit | Branch |
| --- | --- | --- | --- |
| #71 M4 17: Package/bundle purchase invoice | done | `57614e5` | `worktree-agent-a938f512737e03a6b` |

## #71 detail

**What it is.** Buying a `PackageDefinition`/bundle (#60) is its own invoice, issued through
the *exact same* machinery #65 built: the same `business_invoice_counters` row/
`allocate_invoice_number` function (unmodified, called with no new parameters), the same
`Invoice` table, the same `billing.view` checkout capability. `billing/package_purchase.py`
(`POST /api/packages/{definition_id}/purchase`, `GET /api/packages/purchases/{id}`) is the one
writer.

**Schema — migration `0055_package_purchases.py`, revises `0054`.**

- `invoices.service_bill_id` is now nullable; `invoices.package_purchase_id` (nullable, unique,
  `FK package_purchases.id ON DELETE RESTRICT`) is its sibling. `ck_invoices_source_xor`
  (a `CHECK`) pins exactly one of the two to be set on every row — an invoice always has
  exactly one source, never both, never neither.
- `package_purchases` — the frozen purchase snapshot (`PackageDefinition`'s role for a
  service invoice's `ServiceBill`... except there is no draft stage; see below):
  `package_definition_id`/`customer_id` (display/reference FKs, `RESTRICT`), `name`/
  `price_cents` (frozen off the live definition at the moment of sale), `expires_after_days`/
  `expires_at` (frozen policy + the actual computed calendar date, both `NULL` together iff
  the definition never expires), `purchased_at`, and the activation pair
  `credits_activated`/`activated_at` (see below).
- `package_purchase_credits` — one row per credited service (`InvoiceLine`'s role):
  composite PK `(package_purchase_id, service_id)`, `credits_total` (frozen
  `PackageDefinitionService.credits`), `allocated_price_cents` (frozen
  `billing/allocation.py::allocate_bundle_price` output, called exactly once at purchase).

**Why a distinct pair, not `InvoiceLine`.** `InvoiceLine.appointment_id`/`staff_id`/
`service_bill_line_id` are all `NOT NULL` — a package purchase has no appointment, no single
staff member, no `ServiceBillLine`. Forcing it through `InvoiceLine` meant either weakening
that table's contract for every future service invoice, or sentinel FKs. `billing/packages.py`'s
own module docstring had already flagged this exact fork in the road for #71 to resolve;
this ticket takes the distinct-pair branch, referenced by `invoices.package_purchase_id`,
never by `invoice_lines`.

**No draft/review stage.** A service visit accumulates across sibling appointments before
review/issue (#59 -> #63/#64 -> #65). A package purchase has nothing to accumulate — picking a
definition and a customer *is* the transaction. `PackagePurchase` is created and its `Invoice`
issued in the same request/transaction; it carries no `invoice_id` column of its own (the same
one-directional shape `ServiceBill` already has — only `Invoice.package_purchase_id` links
them), so there is no create-order hazard.

**Tax and discounts.** `PackageDefinition.price_cents` gets the same exclusive-convention,
every-active-business-applicable-component tax treatment `bill_review.py` already documents
for a service line — one "line" (the whole purchase), so `Invoice.tax_totals_by_component`
already *is* this purchase's own per-component breakdown; no separate child tax table.
`computed_discount_total_cents` is always `0` — nothing in #71's acceptance criteria asks a
predefined discount (#58) to apply to a package purchase, and there is no review/override
step (`BillOverrideRequest`-equivalent) for one to be authorized through. A later ticket can
add either without touching this shape.

**Credit activation — the ticket's central rule: "a partial payment activates nothing."**
`PackagePurchase.credits_activated` (default `false`) is the sole source of truth for whether
`PackagePurchaseCredit.credits_total` is spendable at all; the frozen total itself is never
zeroed or mutated. `billing/package_purchase.py::activate_credits(db, purchase)` is the *one*
integration point: idempotent, flips `credits_activated` true and stamps `activated_at`.
**Nothing in this ticket calls it anywhere except `tests/test_package_purchase.py`'s own direct
call proving it works.** The purchase route always leaves a freshly created row at
`credits_activated = false` — this was deliberately *not* approximated as "issued == paid"
(unlike #70/receipts, which were told that approximation was acceptable for their ticket): the
acceptance criterion that a partial payment activates nothing is explicit and testable, and
#66 (payment ledger) is not merged yet, so there is no correct moment before "a caller has
proven full payment" to flip it.

**What #66 (or a small follow-up reconciliation ticket) must call**: for each `Invoice` whose
`package_purchase_id IS NOT NULL`, once the payment ledger shows that invoice's
`grand_total_cents` fully collected (#66's own definition of "fully paid" — not redefined
here) and `package_purchase.credits_activated` is still `false`, call
`activate_credits(db, purchase)` inside that same payment-recording transaction. #72 (credit
redemption) is the eventual reader of `credits_activated`/`credits_total` and must refuse to
spend a credit whose purchase is not yet activated.

**Immutability, DB-enforced, mirroring `invoices`' own shape (migration).**
`package_purchases_activation_guard` permits exactly one transition — `credits_activated`
`false` -> `true`, `activated_at` newly set, every other column held bit-for-bit identical —
and refuses every other UPDATE/DELETE from the app role (purge role and table owner bypass, the
same shape `invoices_voidable_guard`/`invoice_lines_no_rewrite` already use). `invoices_
voidable_guard` itself was replaced (`CREATE OR REPLACE`) to compare `service_bill_id`/
`package_purchase_id` with `IS NOT DISTINCT FROM` instead of `=`, now that `service_bill_id`
is nullable — the permitted issued->cancelled transition is otherwise unchanged.
`package_purchase_credits` is fully append-only, `invoice_lines`'s own precedent.

**Files touched:**
- `app/backend/src/billing/models.py` — `Invoice.service_bill_id` nullable +
  `package_purchase_id` + `ck_invoices_source_xor` + `package_purchase` relationship;
  `PackagePurchase`/`PackagePurchaseCredit` appended at the end of the file.
- `app/backend/src/billing/package_purchase.py` — new: `purchase_package` route,
  `get_package_purchase` route, `activate_credits`.
- `app/backend/src/billing/invoices.py` — `InvoiceOut.service_bill_id` now optional,
  `package_purchase_id` added.
- `app/backend/src/main.py` — registers `package_purchase_router`.
- `app/backend/alembic/versions/0055_package_purchases.py` — new migration.
- `app/backend/tests/test_package_purchase.py` — new, 20 tests (TDD, S1 seam).

**Test results:** `tests/test_package_purchase.py` — 20/20 passing. `ruff check`/`ruff format
--check` clean. Full backend suite run at the end of this ticket (see the agent's own final
report for the pass count) to confirm no regression to #65's own `test_invoice_issue.py`
(`service_bill_id` nullability + the replaced trigger function).
