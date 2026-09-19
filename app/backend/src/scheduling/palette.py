"""The curated staff colours, defined once, on the server.

The calendar paints every appointment with its staff member's colour and writes the title on
top of it (tech-stack §13), so this is operational data, not decoration: a colour nobody can
read a title on is an unreadable appointment, and a colour chosen freehand from a picker is
exactly how one gets in. Twelve named colours, and the interface offers these and nothing
else.

**Why white is the text colour to check.** Every hex here is dark enough that white wins
`foreground_for`, and the test in `tests/test_staff.py` keeps that true at 4.5:1 (WCAG AA for
body text). The hues are ordered so that consecutive assignments — which is what a new staff
member gets — land far apart on the wheel rather than as two similar blues.

**The dark hex is derived, not hand-picked.** `settings/branding.py` already knows how to
lift a colour onto a dark surface without shifting its hue; a second table of hand-chosen
dark values would be a second answer to the same question, and the one on screen would be
the one that is wrong.
"""

from dataclasses import dataclass
from functools import cached_property

from settings.branding import derive_dark, foreground_for


@dataclass(frozen=True)
class Colour:
    key: str
    name: str
    # What the event block is painted in on a light calendar. White text on top.
    hex: str

    @cached_property
    def dark_hex(self) -> str:
        """The same colour, light enough to read on a slate-950 page."""
        return derive_dark(self.hex)

    @cached_property
    def foreground(self) -> str:
        return foreground_for(self.hex)

    @cached_property
    def dark_foreground(self) -> str:
        return foreground_for(self.dark_hex)


PALETTE: tuple[Colour, ...] = (
    Colour("blue", "Blue", "#1d4ed8"),
    Colour("teal", "Teal", "#0f766e"),
    Colour("rose", "Rose", "#be123c"),
    Colour("amber", "Amber", "#b45309"),
    Colour("violet", "Violet", "#6d28d9"),
    Colour("green", "Green", "#15803d"),
    Colour("cyan", "Cyan", "#0e7490"),
    Colour("pink", "Pink", "#be185d"),
    Colour("lime", "Lime", "#4d7c0f"),
    Colour("indigo", "Indigo", "#4338ca"),
    Colour("orange", "Orange", "#c2410c"),
    Colour("slate", "Slate", "#475569"),
)

KEYS: tuple[str, ...] = tuple(c.key for c in PALETTE)
BY_KEY: dict[str, Colour] = {c.key: c for c in PALETTE}


def next_free(taken: set[str], active_count: int) -> str:
    """The first colour no active staff member holds.

    Wraps rather than refusing once all twelve are in use: a thirteenth staff member is a
    legitimate thing to have, and a business that has one would rather have two people
    sharing a blue than be told they may not hire.
    """
    for colour in PALETTE:
        if colour.key not in taken:
            return colour.key
    return PALETTE[active_count % len(PALETTE)].key
