"""S2: `billing/tax_table.py`'s built-in Canadian tax table — pure lookup by province and date
(#118, spec #113 "Tax pre-fill"). No database, same split `test_billing_tax.py` already draws.
"""

from datetime import date

from billing.tax_table import components_for_province, find_component, rate_as_of


def test_gst_provinces_get_five_percent_federal_gst():
    resolved = components_for_province("BC", date(2026, 1, 1))
    codes = {c.code for c, _ in resolved}
    assert "GST" in codes
    gst_rate = next(r for c, r in resolved if c.code == "GST")
    assert gst_rate.rate_ppm == 50_000


def test_ontario_gets_only_hst_thirteen_percent():
    resolved = components_for_province("ON", date(2026, 1, 1))
    assert [(c.code, r.rate_ppm) for c, r in resolved] == [("HST", 130000)]


def test_bc_gets_gst_and_pst():
    resolved = components_for_province("BC", date(2026, 1, 1))
    assert sorted((c.code, r.rate_ppm) for c, r in resolved) == [("GST", 50000), ("PST", 70000)]


def test_quebec_gets_gst_and_qst():
    resolved = components_for_province("QC", date(2026, 1, 1))
    # QST is exact at 99_750 ppm (9.975%) — #119, no rounding to 99_800 (9.98%) any more.
    assert sorted((c.code, r.rate_ppm) for c, r in resolved) == [("GST", 50000), ("QST", 99750)]


def test_manitoba_gets_gst_and_rst():
    resolved = components_for_province("MB", date(2026, 1, 1))
    assert sorted((c.code, r.rate_ppm) for c, r in resolved) == [("GST", 50000), ("RST", 70000)]


def test_saskatchewan_gets_gst_and_pst():
    resolved = components_for_province("SK", date(2026, 1, 1))
    assert sorted((c.code, r.rate_ppm) for c, r in resolved) == [("GST", 50000), ("PST", 60000)]


def test_nb_nl_pe_get_fifteen_percent_hst():
    for province in ("NB", "NL", "PE"):
        resolved = components_for_province(province, date(2026, 1, 1))
        assert [(c.code, r.rate_ppm) for c, r in resolved] == [("HST", 150000)]


# --- date boundaries: Nova Scotia's 2025-04-01 rate cut ---------------------------------


def test_nova_scotia_is_fifteen_percent_before_the_cut():
    resolved = components_for_province("NS", date(2025, 3, 31))
    assert [(c.code, r.rate_ppm, r.effective_from) for c, r in resolved] == [
        ("HST", 150000, date(2010, 7, 1))
    ]


def test_nova_scotia_is_fourteen_percent_on_and_after_the_cut():
    resolved = components_for_province("NS", date(2025, 4, 1))
    assert [(c.code, r.rate_ppm, r.effective_from) for c, r in resolved] == [
        ("HST", 140000, date(2025, 4, 1))
    ]


def test_rate_as_of_returns_none_before_the_table_starts():
    component = find_component("GST", "BC")
    assert rate_as_of(component, date(1990, 1, 1)) is None


def test_territories_get_gst_only():
    for territory in ("NT", "NU", "YT"):
        resolved = components_for_province(territory, date(2026, 1, 1))
        assert [(c.code, r.rate_ppm) for c, r in resolved] == [("GST", 50000)]


def test_alberta_gets_gst_only():
    resolved = components_for_province("AB", date(2026, 1, 1))
    assert [(c.code, r.rate_ppm) for c, r in resolved] == [("GST", 50000)]


def test_find_component_returns_none_for_a_province_not_covered():
    assert find_component("PST", "ON") is None


def test_unknown_province_resolves_to_nothing():
    assert components_for_province("ZZ", date(2026, 1, 1)) == []
