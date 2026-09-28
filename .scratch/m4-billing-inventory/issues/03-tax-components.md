# 03 — Tax components & effective-dated rates

**What to build:** Per-item tax components (GST/HST/PST/VAT, toggled individually per catalog
item — never a single `standard|exempt` enum, per CLAUDE.md), sourced from the business's own
address/jurisdiction, with effective-dated rates so a rate change never rewrites a historical
invoice. Catalog and override entry both state their tax convention (inclusive or exclusive)
and the resolved total is shown before issue. (#54 stories 30-33.)

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] Tax components are effective-dated data, selected from the business's jurisdiction
      (province/address), not hardcoded
- [ ] Each catalog item toggles each applicable component independently
- [ ] Catalog/override entry is explicitly labelled tax-inclusive or tax-exclusive; both
      compute the same final total, and inclusive-price extraction reconciles exactly to the
      cent
- [ ] Tax is computed per line, rounded half-up, then summed — integer cents throughout
- [ ] An issued invoice snapshots the resolved components/rates/amounts/convention at issue
      time; a later rate change never alters an already-issued invoice's numbers
- [ ] Service tax exemption is never inferred from a practitioner's profession
