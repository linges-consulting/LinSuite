"""Brand colours: the dark-theme variant, the readable foreground, and WCAG contrast.

One implementation, here, in Python. The Branding screen's live preview asks the server for
the same numbers `GET /api/branding` will serve, rather than carrying a second copy of this
arithmetic in TypeScript — two copies of a colour-space conversion is two answers to "what
colour is this on dark?", and the one on screen would be the one that is wrong.

**Why a perceptual space.** A dark brand colour is unreadable on slate-950, so the dark theme
needs a lighter variant of it. Lightening in sRGB (`#rrggbb` scaled toward `#ffffff`) shifts
hue — a blue goes violet, a red goes pink — because sRGB's channels are not perceptually
uniform. Oklab's `L` axis is, so raising `L` to a fixed target and keeping the hue angle
produces the same *colour*, lighter. Chroma is pulled back a little with the lightening
(`_CHROMA_KEPT`), because a fully saturated tint at high lightness reads as neon.

**Contrast is reported, never enforced.** The foreground is chosen as whichever of white and
near-black reads better on the colour; when even that is below 4.5:1 the screen says so and
saves anyway. It is the business's own brand, and refusing it would be this application
deciding what a logo may look like.
"""

import re
from dataclasses import dataclass

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

# The shipped theme is tweakcn's Stillwater (docs/DESIGN.md): its slate-blue primary and its
# sage-green secondary are what a business starts on until it picks its own.
DEFAULT_PRIMARY = "#1a6289"
DEFAULT_SECONDARY = "#2f7a5c"

# The two foregrounds the design system has: white, and slate-900 (`--foreground`).
WHITE = "#ffffff"
NEAR_BLACK = "#0f172a"

# Where a dark-theme variant lands on Oklab's lightness axis. 0.72 is where DESIGN.md's own
# pre-Stillwater default pair sat: `#1d4ed8` derived to `#659dff`, against a hand-picked `#60a5fa`.
_DARK_L = 0.72
# How much of the colour's chroma survives the lightening. All of it reads as neon up there.
_CHROMA_KEPT = 0.9


def is_hex(value: str) -> bool:
    return bool(HEX.match(value))


def normalise(value: str) -> str:
    """The one spelling stored and served: lowercase, six digits, leading `#`."""
    return value.lower()


def _channels(colour: str) -> tuple[float, float, float]:
    return tuple(int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]


def _to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _to_srgb(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def _oklab(colour: str) -> tuple[float, float, float]:
    r, g, b = (_to_linear(c) for c in _channels(colour))
    lms = (
        0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
        0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
        0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b,
    )
    l_, m_, s_ = (v ** (1 / 3) for v in lms)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def _hex(lightness: float, a: float, b: float) -> str:
    l_ = lightness + 0.3963377774 * a + 0.2158037573 * b
    m_ = lightness - 0.1055613458 * a - 0.0638541728 * b
    s_ = lightness - 0.0894841775 * a - 1.2914855480 * b
    lo, mo, so = l_**3, m_**3, s_**3
    linear = (
        4.0767416621 * lo - 3.3077115913 * mo + 0.2309699292 * so,
        -1.2684380046 * lo + 2.6097574011 * mo - 0.3413193965 * so,
        -0.0041960863 * lo - 0.7034186147 * mo + 1.7076147010 * so,
    )
    return "#" + "".join(f"{round(255 * min(1.0, max(0.0, _to_srgb(v)))):02x}" for v in linear)


def derive_dark(colour: str) -> str:
    """The same colour, light enough to read on a slate-950 page."""
    lightness, a, b = _oklab(colour)
    if lightness >= _DARK_L:
        return normalise(colour)
    kept = _CHROMA_KEPT
    return _hex(_DARK_L, a * kept, b * kept)


def _luminance(colour: str) -> float:
    r, g, b = (_to_linear(c) for c in _channels(colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(one: str, other: str) -> float:
    """WCAG 2.1 contrast ratio, 1.0 – 21.0."""
    a, b = sorted((_luminance(one), _luminance(other)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def foreground_for(colour: str) -> str:
    """Whichever of the two text colours reads better on this one."""
    return max((WHITE, NEAR_BLACK), key=lambda fg: contrast(colour, fg))


@dataclass(frozen=True)
class Palette:
    """What `<html>` gets its four brand variables from, plus the numbers the screen warns on."""

    primary: str
    primary_foreground: str
    primary_dark: str
    primary_dark_foreground: str
    secondary: str
    secondary_foreground: str
    secondary_dark: str
    secondary_dark_foreground: str

    def colors(self) -> dict[str, str]:
        return self.__dict__.copy()

    def contrast(self) -> dict[str, float]:
        """Text on each brand surface, in the theme it is used in. Below 4.5 is a warning."""
        return {
            "primary": round(contrast(self.primary, self.primary_foreground), 2),
            "primary_dark": round(contrast(self.primary_dark, self.primary_dark_foreground), 2),
            "secondary": round(contrast(self.secondary, self.secondary_foreground), 2),
            "secondary_dark": round(
                contrast(self.secondary_dark, self.secondary_dark_foreground), 2
            ),
        }


def palette(primary: str, secondary: str) -> Palette:
    primary, secondary = normalise(primary), normalise(secondary)
    primary_dark, secondary_dark = derive_dark(primary), derive_dark(secondary)
    return Palette(
        primary=primary,
        primary_foreground=foreground_for(primary),
        primary_dark=primary_dark,
        primary_dark_foreground=foreground_for(primary_dark),
        secondary=secondary,
        secondary_foreground=foreground_for(secondary),
        secondary_dark=secondary_dark,
        secondary_dark_foreground=foreground_for(secondary_dark),
    )
