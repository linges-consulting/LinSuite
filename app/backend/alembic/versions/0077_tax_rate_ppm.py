"""Tax rates in parts per million, not basis points (#119): QST's 9.975% is 99_750 ppm exactly,
where it was 998 whole basis points (9.98%, an over-charge) before.

Revision ID: 0077
Revises: 0075
Create Date: 2026-09-29

Every stored tax rate moves from basis points (1% = 100 bp) to parts per million (1% =
10_000 ppm) — a ×100 scale change that keeps every existing whole-bp rate's cents identical
(`tests/test_billing_tax.py`) while finally giving a fractional rate like QST's an exact
integer representation. Four places carry a tax rate:

- `tax_component_rates.rate_bp` — the effective-dated rate an admin sets (`billing/tax_routes.py`).
- `invoice_line_taxes.rate_bp` / `retail_invoice_line_taxes.rate_bp` — the resolved rate frozen
  onto each issued line at #65's own snapshot time.
- `invoices.tax_rates_by_component` — a JSONB `{component_code: rate}` map frozen on a service
  invoice at issue (package-purchase invoices use this too, `billing/package_purchase.py`); no
  column rename here since the column was never named `*_bp`, only its stored values change.

Three of the four are `*_bp` columns, renamed to `*_ppm` in place (`ALTER TABLE ... RENAME
COLUMN`, metadata only — no row touched, no trigger fired) and then multiplied ×100 with an
ordinary `UPDATE`. `invoice_line_taxes` and `retail_invoice_line_taxes` are append-only,
trigger-guarded tables (`0067_purge_role_default_deny.py`'s own `_APPEND_ONLY` guards); their
`UPDATE` is permitted here only because every guard's bypass is "the current role owns the
table" (`current_user = (SELECT pg_get_userbyid(relowner) ...)`), which is exactly what this
migration runs as — no trigger needs touching. `tax_component_rates` carries no such guard at
all (a rate is effective-dated data, not an issued snapshot). `invoices` does carry
`snapshot_columns_frozen`, guarding `tax_rates_by_component` among other columns, but the same
owner bypass applies.

Every CHECK bound tied to a renamed column moves from `BETWEEN 0 AND 10000` to
`BETWEEN 0 AND 1000000` (`billing/models.py::MAX_TAX_RATE_PPM`); a `_bp`-suffixed constraint
name is renamed alongside its column, one that was never suffixed (`ck_retail_invoice_line_
taxes_rate`) keeps its name.

**Deliberately untouched:** `commission_rate_services_bp`/`commission_rate_retail_bp`
(`staff`), every `commission_rate_bp` (`service_bill_lines`, `invoice_lines`,
`retail_invoice_lines`, `commission_postings`) and `percentage_bp` (`discounts`,
`invoice_line_discounts`, `retail_invoice_line_discounts`) — commission rates and discount
percentages, not tax, and CLAUDE.md's basis-point convention is correct for both: neither needs
a fractional-of-a-percent rate the way QST does.

The downgrade divides by 100 (exact — every migrated value is a multiple of 100) and renames
each column back.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0077"
down_revision: str | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, old check name, new check name)
_RENAMED_RATE_CHECKS = (
    ("tax_component_rates", "ck_tax_component_rates_bp", "ck_tax_component_rates_ppm"),
    ("invoice_line_taxes", "ck_invoice_line_taxes_rate_bp", "ck_invoice_line_taxes_rate_ppm"),
)
# (table, check name) — bound moves, name does not.
_UNRENAMED_RATE_CHECKS = (("retail_invoice_line_taxes", "ck_retail_invoice_line_taxes_rate"),)

_RATE_TABLES = (
    "tax_component_rates",
    "invoice_line_taxes",
    "retail_invoice_line_taxes",
)


def upgrade() -> None:
    for table, old_name, _ in _RENAMED_RATE_CHECKS:
        op.drop_constraint(old_name, table, type_="check")
    for table, name in _UNRENAMED_RATE_CHECKS:
        op.drop_constraint(name, table, type_="check")

    for table in _RATE_TABLES:
        op.alter_column(table, "rate_bp", new_column_name="rate_ppm")
        op.execute(f"UPDATE {table} SET rate_ppm = rate_ppm * 100")

    for table, _, new_name in _RENAMED_RATE_CHECKS:
        op.create_check_constraint(new_name, table, "rate_ppm BETWEEN 0 AND 1000000")
    for table, name in _UNRENAMED_RATE_CHECKS:
        op.create_check_constraint(name, table, "rate_ppm BETWEEN 0 AND 1000000")

    # `invoices.tax_rates_by_component`: a JSONB `{code: rate}` map, never `*_bp`-named, so no
    # column rename — only the stored numbers move. `COALESCE` keeps an empty `'{}'::jsonb`
    # empty rather than turning it `NULL` (`jsonb_object_agg` over zero rows is `NULL`).
    op.execute(
        "UPDATE invoices SET tax_rates_by_component = COALESCE("
        "  (SELECT jsonb_object_agg(key, (value::numeric * 100)::bigint)"
        "     FROM jsonb_each(tax_rates_by_component)),"
        "  '{}'::jsonb"
        ") WHERE tax_rates_by_component <> '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE invoices SET tax_rates_by_component = COALESCE("
        "  (SELECT jsonb_object_agg(key, (value::numeric / 100)::bigint)"
        "     FROM jsonb_each(tax_rates_by_component)),"
        "  '{}'::jsonb"
        ") WHERE tax_rates_by_component <> '{}'::jsonb"
    )

    for table, _old_name, new_name in _RENAMED_RATE_CHECKS:
        op.drop_constraint(new_name, table, type_="check")
    for table, name in _UNRENAMED_RATE_CHECKS:
        op.drop_constraint(name, table, type_="check")

    for table in _RATE_TABLES:
        op.execute(f"UPDATE {table} SET rate_ppm = rate_ppm / 100")
        op.alter_column(table, "rate_ppm", new_column_name="rate_bp")

    for table, old_name, _ in _RENAMED_RATE_CHECKS:
        op.create_check_constraint(old_name, table, "rate_bp BETWEEN 0 AND 10000")
    for table, name in _UNRENAMED_RATE_CHECKS:
        op.create_check_constraint(name, table, "rate_bp BETWEEN 0 AND 10000")
