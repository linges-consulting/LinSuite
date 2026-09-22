"""S2: `classify` is the whole rule (pre-flight D8) — a pure function, tested without a
database. The HTTP-level assertions (the threshold setting, completed-only counting,
one-query-per-list) live in `tests/test_customer_profile.py`.
"""

from customers.classification import classify


def test_zero_completed_visits_is_new():
    assert classify(0, vip_threshold=10) == "new"


def test_one_completed_visit_is_repeat():
    assert classify(1, vip_threshold=10) == "repeat"


def test_one_below_the_threshold_is_still_repeat():
    assert classify(9, vip_threshold=10) == "repeat"


def test_exactly_the_threshold_is_vip():
    assert classify(10, vip_threshold=10) == "vip"


def test_past_the_threshold_is_vip():
    assert classify(50, vip_threshold=10) == "vip"


def test_a_lowered_threshold_reclassifies_the_same_count():
    assert classify(3, vip_threshold=10) == "repeat"
    assert classify(3, vip_threshold=3) == "vip"
