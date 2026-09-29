"""S2: `normalize_phone` is the whole rule (Phase 14, #16) — a pure function, tested without a
database. The matching behaviour (partial digits, formatted input, the functional index) is
proven at S1 in `tests/test_cti.py`.
"""

from customers.phone import normalize_phone


def test_bare_ten_digits_is_unchanged():
    assert normalize_phone("4165550199") == "4165550199"


def test_formatting_is_stripped():
    assert normalize_phone("(416) 555-0199") == "4165550199"
    assert normalize_phone("416.555.0199") == "4165550199"
    assert normalize_phone("416-555-0199") == "4165550199"


def test_a_leading_nanp_country_code_is_dropped():
    assert normalize_phone("+1 (416) 555-0199") == "4165550199"
    assert normalize_phone("1-416-555-0199") == "4165550199"
    assert normalize_phone("14165550199") == "4165550199"


def test_an_eleven_digit_number_not_starting_with_one_is_left_alone():
    """Not a valid NANP country code, so there is nothing to strip — an 11-digit local number
    from another plan is passed through rather than guessed at."""
    assert normalize_phone("24165550199") == "24165550199"


def test_none_and_blank_normalise_to_the_empty_string():
    assert normalize_phone(None) == ""
    assert normalize_phone("") == ""
    assert normalize_phone("   ") == ""


def test_partial_digits_normalise_the_same_way_a_full_number_does():
    """What a receptionist has typed so far, one digit at a time — no leading-1 stripping
    kicks in until there are eleven digits to strip from."""
    assert normalize_phone("416") == "416"
    assert normalize_phone("1-416") == "1416"
