# 22 — Retail returns

**What to build:** Refunding money and returning goods to stock as two independent choices —
an opened or damaged product can be refunded without restocking it, while a suitable return
explicitly restocks via a whole-unit return movement. Refunds still route through #13's
admin-approval path. (#54 stories 81-82.)

**Blocked by:** 21 (a retail sale to return), 13 (the refund-approval path)

**Status:** ready-for-agent

- [ ] Refunding money and returning stock are two independently-made choices on the same
      return action — one never implies the other
- [ ] A suitable return writes a whole-unit return movement (#07) restocking the item; an
      unsuitable (opened/damaged) return does not
- [ ] The money side always routes through #13's admin-approved refund, respecting its cap
- [ ] A return against an anonymous retail sale works the same as one against a client-linked
      sale
