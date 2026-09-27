"""S2: the signature pad's ink thresholds stay in step with the server's (fix round 1, #51).

`signature-pad.tsx` mirrors `forms/submissions.py`'s `_has_ink` thresholds by value — a true
shared runtime import was tried and reverted, because the frontend's dev/prod containers only
bind-mount `app/frontend` (`infra/compose.yaml`), so a cross-directory import 404s inside the
real container even though it resolves fine against a full checkout on the host (or in this
test). `app/shared/signature-ink-constants.json` is the canonical reference instead — the same
"shared JSON fixture, not shared runtime code" shape `form-schema-cases.json` already uses for
the schema twins — and this test fails if either twin's own numbers drift from it.
"""

import json
import re
from pathlib import Path

BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
SHARED = Path(__file__).resolve().parents[2] / "shared" / "signature-ink-constants.json"

CANONICAL = json.loads(SHARED.read_text())
NAMES = ("MIN_INK_PIXELS", "MIN_INK_WIDTH", "MIN_INK_HEIGHT", "MAX_INK_RATIO")


def _python_constants() -> dict[str, float]:
    from forms.submissions import MAX_INK_RATIO, MIN_INK_HEIGHT, MIN_INK_PIXELS, MIN_INK_WIDTH

    return {
        "MIN_INK_PIXELS": MIN_INK_PIXELS,
        "MIN_INK_WIDTH": MIN_INK_WIDTH,
        "MIN_INK_HEIGHT": MIN_INK_HEIGHT,
        "MAX_INK_RATIO": MAX_INK_RATIO,
    }


def _ts_constants() -> dict[str, float]:
    """`forms.submissions` can be imported; `signature-pad.tsx` cannot — parsed from its
    source text instead, the same way this suite would notice a typo'd number either way."""
    source = (FRONTEND_SRC / "components" / "signature-pad.tsx").read_text()
    found = {}
    for name in NAMES:
        match = re.search(rf"^const {name} = ([\d.]+)$", source, re.MULTILINE)
        assert match, f"{name} not found (as a plain `const NAME = number`) in signature-pad.tsx"
        found[name] = float(match.group(1))
    return found


def test_the_backend_constants_match_the_shared_canonical_values():
    assert _python_constants() == CANONICAL


def test_the_signature_pad_constants_match_the_shared_canonical_values():
    assert _ts_constants() == {k: float(v) for k, v in CANONICAL.items()}


def test_backend_src_directory_is_where_this_test_thinks_it_is():
    # A cheap guard against the path arithmetic above silently finding nothing and both
    # `_python_constants`/`_ts_constants` importing or matching against stale, unrelated files.
    assert (BACKEND_SRC / "forms" / "submissions.py").is_file()
    assert (FRONTEND_SRC / "components" / "signature-pad.tsx").is_file()
