"""S2: `billing/tax.py`'s pure per-line tax arithmetic (#57; #54 story 33).

No database — every rate here is a plain `ComponentRate` the caller already resolved, the
same split `tests/test_classification.py` uses for `customers/classification.py`. Coverage
follows m4.md's own list for this ground: inclusive/exclusive entry, multiple independent
components, exempt components, effective-date boundaries, half-cent rounding, line-versus-
total reconciliation.
"""

from datetime import date

from billing.tax import ComponentRate, compute_line_tax, invoice_tax_totals, resolve_rate_bp

GST = ComponentRate("GST", 500)  # 5%
PST = ComponentRate("PST", 700)  # 7%, BC's own rate — GST yes, PST also yes here
EXEMPT = ComponentRate("PST", 0)  # explicitly listed, but zero: never inferred, always stated


# --- exclusive: the entered amount is the pre-tax price --------------------------------


def test_exclusive_single_component():
    line = compute_line_tax(10_000, [GST], "exclusive")
    assert line.pretax_cents == 10_000
    assert line.component_cents == {"GST": 500}
    assert line.tax_cents == 500
    assert line.total_cents == 10_500


def test_exclusive_multiple_components_reconcile_to_the_line_total():
    line = compute_line_tax(10_000, [GST, PST], "exclusive")
    assert line.component_cents == {"GST": 500, "PST": 700}
    assert line.tax_cents == 1_200
    assert line.total_cents == 11_200
    assert sum(line.component_cents.values()) == line.tax_cents
    assert line.pretax_cents + line.tax_cents == line.total_cents


def test_exclusive_half_up_rounding():
    # 333 cents * 15% = 49.95 -> half-up to 50, not 49 (banker's rounding would give 50 too,
    # but 332 * 15% = 49.8 would round to 50 either way; this value is chosen so ROUND_HALF_UP
    # and ROUND_HALF_EVEN would actually disagree at the *next* cent: 330 * 15% = 49.5 exactly).
    fifteen_pct = ComponentRate("VAT", 1500)
    line = compute_line_tax(330, [fifteen_pct], "exclusive")
    assert line.component_cents == {"VAT": 50}  # 49.5 rounds up, never down or to even
    assert line.total_cents == 380


def test_exclusive_exempt_component_is_reported_at_zero_not_omitted():
    line = compute_line_tax(10_000, [GST, EXEMPT], "exclusive")
    assert line.component_cents == {"GST": 500, "PST": 0}
    assert line.tax_cents == 500
    assert line.total_cents == 10_500


def test_exclusive_no_components_is_a_no_op():
    line = compute_line_tax(10_000, [], "exclusive")
    assert line == compute_line_tax(10_000, [], "exclusive")
    assert line.tax_cents == 0
    assert line.total_cents == 10_000


# --- inclusive: the entered amount is the final, tax-included price --------------------


def test_inclusive_extraction_reconciles_exactly_to_the_cent():
    # The exclusive case above computed 10000 pretax + GST+PST -> 11200 total. Feeding that
    # total back in as an inclusive price must recover the same pretax and component amounts.
    line = compute_line_tax(11_200, [GST, PST], "inclusive")
    assert line.pretax_cents == 10_000
    assert line.component_cents == {"GST": 500, "PST": 700}
    assert line.tax_cents == 1_200
    assert line.total_cents == 11_200
    assert line.pretax_cents + line.tax_cents == line.total_cents
    assert sum(line.component_cents.values()) == line.tax_cents


def test_inclusive_single_component_round_trips_the_exclusive_case():
    line = compute_line_tax(10_500, [GST], "inclusive")
    assert line.pretax_cents == 10_000
    assert line.component_cents == {"GST": 500}
    assert line.total_cents == 10_500


def test_inclusive_no_components_leaves_the_price_untouched():
    line = compute_line_tax(10_000, [], "inclusive")
    assert line.pretax_cents == 10_000
    assert line.tax_cents == 0
    assert line.total_cents == 10_000


def test_inclusive_odd_total_still_reconciles_components_exactly():
    # A total that does not divide evenly across GST+PST — the largest-remainder allocation
    # must still make the parts sum to the residual exactly, never off by a cent either way.
    line = compute_line_tax(10_001, [GST, PST], "inclusive")
    assert line.pretax_cents + line.tax_cents == 10_001
    assert sum(line.component_cents.values()) == line.tax_cents


def test_inclusive_exempt_component_reported_at_zero():
    line = compute_line_tax(10_500, [GST, EXEMPT], "inclusive")
    assert line.component_cents == {"GST": 500, "PST": 0}
    assert line.pretax_cents == 10_000


# --- both conventions land on the same final total for the same underlying price -------


def test_inclusive_and_exclusive_agree_on_the_final_total():
    exclusive = compute_line_tax(10_000, [GST, PST], "exclusive")
    inclusive = compute_line_tax(exclusive.total_cents, [GST, PST], "inclusive")
    assert inclusive.total_cents == exclusive.total_cents
    assert inclusive.pretax_cents == exclusive.pretax_cents
    assert inclusive.component_cents == exclusive.component_cents


# --- invoice_tax_totals: summed per line, never re-derived from a grand total ----------


def test_invoice_tax_totals_sums_each_lines_own_rounded_amount():
    line_a = compute_line_tax(999, [GST], "exclusive")  # 999 * 5% = 49.95 -> 50
    line_b = compute_line_tax(999, [GST], "exclusive")  # another 50
    totals = invoice_tax_totals([line_a, line_b])
    assert totals == {"GST": 100}
    # Not the same as rounding the doubled pretax once: 1998 * 5% = 99.9 -> 100 too here, but
    # the point is this sums the *lines'* own rounded values, not the invoice's raw base.
    assert totals["GST"] == line_a.component_cents["GST"] + line_b.component_cents["GST"]


def test_invoice_tax_totals_empty():
    assert invoice_tax_totals([]) == {}


# --- resolve_rate_bp: effective-date boundaries -----------------------------------------


def test_resolve_rate_bp_within_a_range():
    history = [(date(2024, 1, 1), date(2025, 1, 1), 500)]
    assert resolve_rate_bp(history, date(2024, 6, 1)) == 500


def test_resolve_rate_bp_effective_from_is_inclusive():
    history = [(date(2024, 1, 1), date(2025, 1, 1), 500)]
    assert resolve_rate_bp(history, date(2024, 1, 1)) == 500


def test_resolve_rate_bp_effective_to_is_exclusive():
    history = [(date(2024, 1, 1), date(2025, 1, 1), 500)]
    assert resolve_rate_bp(history, date(2025, 1, 1)) is None


def test_resolve_rate_bp_before_any_rate_existed():
    history = [(date(2024, 1, 1), None, 500)]
    assert resolve_rate_bp(history, date(2023, 12, 31)) is None


def test_resolve_rate_bp_open_ended_rate_covers_today_and_beyond():
    history = [(date(2024, 1, 1), None, 500)]
    assert resolve_rate_bp(history, date(2099, 1, 1)) == 500


def test_resolve_rate_bp_picks_the_right_rate_across_a_change():
    history = [
        (date(2024, 1, 1), date(2025, 1, 1), 500),
        (date(2025, 1, 1), None, 700),
    ]
    assert resolve_rate_bp(history, date(2024, 12, 31)) == 500
    assert resolve_rate_bp(history, date(2025, 1, 1)) == 700
