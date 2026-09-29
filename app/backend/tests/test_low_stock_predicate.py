"""S2: the pure low-stock predicate (#62). No database, no client fixture — plain pytest
against `inventory/stock.py::is_below_threshold`, per CLAUDE.md's own testing-seams split.
"""

from inventory.stock import is_below_threshold


def test_above_threshold_is_not_low():
    assert is_below_threshold(10, 5) is False


def test_exactly_at_threshold_is_not_low():
    # At the threshold is still "enough to reorder at", not yet below it.
    assert is_below_threshold(5, 5) is False


def test_below_threshold_is_low():
    assert is_below_threshold(4, 5) is True


def test_zero_threshold_never_low_for_any_nonnegative_count():
    assert is_below_threshold(0, 0) is False
