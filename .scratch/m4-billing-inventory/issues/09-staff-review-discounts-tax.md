# 09 — Staff bill review: discounts + tax totals

**What to build:** The staff-facing draft-bill screen: apply any enabled, eligible predefined
discount (#04) to the bill's lines without approval, and see the resolved total including tax
(#03) recomputed live as discounts are added or removed. This is the first point the three
earlier standalone primitives (draft bill, discounts, tax) come together into one demoable
screen. (#54 stories 8, 20, 22-24, 30-31.)

**Blocked by:** 05 (a draft bill to review), 04 (discounts to apply), 03 (tax to total)

**Status:** ready-for-agent

- [ ] Staff sees the draft bill's lines (from #05) and every enabled discount eligible for at
      least one of them (from #04)
- [ ] Applying an eligible, stackable combination recomputes the total using #04's resolver
      (percentage-then-fixed, deterministic)
- [ ] The displayed total includes tax computed per #03's rules, with the catalog's stated
      convention (inclusive/exclusive) honoured
- [ ] An excessive/rejected combination shows the specific reason, not a silent no-op
- [ ] Nothing on this screen requires admin/owner approval yet — that's #10
