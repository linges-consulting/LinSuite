"""S2: `new` / `repeat` / `vip`, derived — never stored (pre-flight D8).

One count (a client's `completed` appointments — cancelled and no-show never count) against
one business setting (`vip_visit_threshold`) is the whole rule. Keeping it a pure function
rather than a stored tag is what makes "lower the threshold" take effect on the very next
read, with no recompute job and nothing to drift.
"""

from typing import Literal

Classification = Literal["new", "repeat", "vip"]


def classify(completed_visits: int, vip_threshold: int) -> Classification:
    if completed_visits <= 0:
        return "new"
    if completed_visits >= vip_threshold:
        return "vip"
    return "repeat"
