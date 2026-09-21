"""S2: `scheduling.availability.chain_starts`, the sequential search over what the engine has
already answered per link (tech-stack §19's "group bookings need sequential slot search").

Pure — plain `Slot`s in, `ChainStart`s out, no session, no clock. `test_groups.py` is where a
chain gets a database and an HTTP request; this file is where the walk itself is pinned.
"""

from datetime import UTC, datetime

from scheduling.availability import ChainLink, ChainStart, Slot, chain_starts

ANA = "ana"
BEN = "ben"
CID = "cid"


def at(hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 1, 5, hh, mm, tzinfo=UTC)


def slot(start_hh: int, end_hh: int, *staff: str, start_mm: int = 0, end_mm: int = 0) -> Slot:
    return Slot(starts_at=at(start_hh, start_mm), ends_at=at(end_hh, end_mm), staff_ids=staff)


def test_a_two_link_chain_matches_the_second_start_to_the_first_ends_at():
    # Facial 09:00-10:00 with Ana, massage must start exactly at 10:00.
    links = [
        ChainLink(slots=[slot(9, 10, ANA)], staff_id=ANA),
        ChainLink(slots=[slot(10, 11, BEN), slot(11, 12, BEN)], staff_id=BEN),
    ]
    assert chain_starts(links) == [ChainStart(starts_at=at(9), staff_ids=(ANA, BEN))]


def test_a_second_link_with_no_slot_at_the_handover_breaks_the_chain():
    # Ben is free 11:00-12:00, but the facial ends at 10:00 — no slot starts there.
    links = [
        ChainLink(slots=[slot(9, 10, ANA)], staff_id=ANA),
        ChainLink(slots=[slot(11, 12, BEN)], staff_id=BEN),
    ]
    assert chain_starts(links) == []


def test_three_link_chain_walks_every_handover():
    links = [
        ChainLink(slots=[slot(9, 10, ANA)], staff_id=ANA),
        ChainLink(slots=[slot(10, 11, BEN)], staff_id=BEN),
        ChainLink(slots=[slot(11, 12, CID)], staff_id=CID),
    ]
    assert chain_starts(links) == [ChainStart(starts_at=at(9), staff_ids=(ANA, BEN, CID))]


def test_three_link_chain_breaks_when_the_middle_link_cannot_continue():
    # Cid is only free 12:00-13:00, one hour after the massage (10:00-11:00) ends.
    links = [
        ChainLink(slots=[slot(9, 10, ANA)], staff_id=ANA),
        ChainLink(slots=[slot(10, 11, BEN)], staff_id=BEN),
        ChainLink(slots=[slot(12, 13, CID)], staff_id=CID),
    ]
    assert chain_starts(links) == []


def test_any_resolves_to_the_lowest_sort_order_member_the_slot_offers():
    # "Any" for link 2, ordered Ana-then-Ben — the slot at 10:00 only Ben holds, so Ben wins
    # even though Ana is first in the order; a different slot could still have gone to Ana.
    links = [
        ChainLink(slots=[slot(9, 10, ANA)], staff_id=ANA),
        ChainLink(slots=[slot(10, 11, BEN)], staff_id=None, staff_order=(ANA, BEN)),
    ]
    assert chain_starts(links) == [ChainStart(starts_at=at(9), staff_ids=(ANA, BEN))]


def test_any_prefers_the_lowest_sort_order_when_both_are_offered():
    links = [
        ChainLink(slots=[slot(9, 10, ANA)], staff_id=ANA),
        ChainLink(slots=[slot(10, 11, ANA, BEN)], staff_id=None, staff_order=(BEN, ANA)),
    ]
    assert chain_starts(links) == [ChainStart(starts_at=at(9), staff_ids=(ANA, BEN))]


def test_a_busy_second_link_staff_member_drops_that_start_but_not_others():
    # Ana is free 09:00-10:00 and 10:00-11:00; Ben (the only one eligible for link 2) is only
    # free starting at 11:00 — so only the 10:00 start chains, not the 09:00 one.
    links = [
        ChainLink(slots=[slot(9, 10, ANA), slot(10, 11, ANA)], staff_id=ANA),
        ChainLink(slots=[slot(11, 12, BEN)], staff_id=BEN),
    ]
    assert chain_starts(links) == [ChainStart(starts_at=at(10), staff_ids=(ANA, BEN))]


def test_no_links_is_no_starts():
    assert chain_starts([]) == []


def test_a_single_link_chain_is_just_that_links_own_starts():
    links = [ChainLink(slots=[slot(9, 10, ANA), slot(10, 11, ANA)], staff_id=ANA)]
    assert chain_starts(links) == [
        ChainStart(starts_at=at(9), staff_ids=(ANA,)),
        ChainStart(starts_at=at(10), staff_ids=(ANA,)),
    ]
