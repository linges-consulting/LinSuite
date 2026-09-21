"""S2: `scheduling.availability.waive_handover`, the pure handover-buffer waiver (fix round
2). Same client, no turnover between two links of one visit — *derived* fresh from current
position and status, never stored: `tests/test_groups.py` is where cancelling or moving a
sibling is proven to "restore" the buffer with no code of its own, because this is the one
place that ever decides the waiver applies.
"""

from datetime import UTC, datetime

from scheduling.availability import Occupant, waive_handover

GROUP = "g1"
OTHER_GROUP = "g2"


def at(hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 1, 5, hh, mm, tzinfo=UTC)


def occupant(
    id_: str,
    *,
    starts_at: datetime,
    ends_at: datetime,
    group: str | None = GROUP,
    status: str = "confirmed",
) -> Occupant:
    return Occupant(
        id=id_, booking_group_id=group, status=status, starts_at=starts_at, ends_at=ends_at
    )


def test_an_adjacent_occupying_sibling_waives_the_facing_edge():
    # Facial 9-10, addon 10-10:30, same group: facial's after-edge and addon's before-edge
    # both face the shared 10:00 instant.
    facial = occupant("facial", starts_at=at(9), ends_at=at(10))
    addon = occupant("addon", starts_at=at(10), ends_at=at(10, 30))

    facial_before, facial_after = waive_handover(facial, [addon], 5, 15)
    addon_before, addon_after = waive_handover(addon, [facial], 10, 20)

    assert (facial_before, facial_after) == (5, 0)  # only the facing (after) edge waived
    assert (addon_before, addon_after) == (0, 20)  # only the facing (before) edge waived


def test_a_non_adjacent_sibling_in_the_same_group_does_not_waive_anything():
    # Same group, but a gap between them — not a handover, full buffers both sides.
    facial = occupant("facial", starts_at=at(9), ends_at=at(10))
    addon = occupant("addon", starts_at=at(10, 15), ends_at=at(10, 45))

    assert waive_handover(facial, [addon], 5, 15) == (5, 15)
    assert waive_handover(addon, [facial], 10, 20) == (10, 20)


def test_a_cancelled_sibling_does_not_waive_anything():
    facial = occupant("facial", starts_at=at(9), ends_at=at(10))
    addon = occupant("addon", starts_at=at(10), ends_at=at(10, 30), status="cancelled")

    assert waive_handover(facial, [addon], 5, 15) == (5, 15)


def test_a_no_show_sibling_does_not_waive_anything():
    facial = occupant("facial", starts_at=at(9), ends_at=at(10))
    addon = occupant("addon", starts_at=at(10), ends_at=at(10, 30), status="no_show")

    assert waive_handover(facial, [addon], 5, 15) == (5, 15)


def test_a_touching_appointment_in_a_different_group_does_not_waive_anything():
    facial = occupant("facial", starts_at=at(9), ends_at=at(10))
    outsider = occupant("outsider", starts_at=at(10), ends_at=at(10, 30), group=OTHER_GROUP)

    assert waive_handover(facial, [outsider], 5, 15) == (5, 15)


def test_a_subject_outside_any_group_is_never_waived():
    subject = occupant("solo", starts_at=at(9), ends_at=at(10), group=None)
    touching = occupant("other", starts_at=at(10), ends_at=at(10, 30))

    assert waive_handover(subject, [touching], 5, 15) == (5, 15)


def test_the_subject_itself_in_the_others_list_is_never_its_own_sibling():
    subject = occupant("solo", starts_at=at(9), ends_at=at(10))

    assert waive_handover(subject, [subject], 5, 15) == (5, 15)


def test_a_middle_link_can_have_both_edges_waived_by_different_siblings():
    a = occupant("a", starts_at=at(8), ends_at=at(9))
    b = occupant("b", starts_at=at(9), ends_at=at(10))
    c = occupant("c", starts_at=at(10), ends_at=at(11))

    assert waive_handover(b, [a, c], 5, 15) == (0, 0)


def test_a_cancelled_subject_is_never_waived_either():
    facial = occupant("facial", starts_at=at(9), ends_at=at(10), status="cancelled")
    addon = occupant("addon", starts_at=at(10), ends_at=at(10, 30))

    assert waive_handover(facial, [addon], 5, 15) == (5, 15)
