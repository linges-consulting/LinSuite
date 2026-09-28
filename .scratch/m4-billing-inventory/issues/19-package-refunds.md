# 19 — Package/bundle refunds

**What to build:** The standard policy — a package/bundle with zero redeemed credits is
refundable for the amount paid, minus any prior refund, and cancels its unused entitlement.
Once any credit is used, it becomes nonrefundable under the standard policy; only an explicit
admin/owner manual exception (with a reason) can refund it after that, choosing separately
whether to keep or cancel remaining credits and whether to preserve or reverse earned
commission. All refunds respect #13's payments-received cap across any replacement lineage.
(#54 stories 60-65; explicitly reverses the old "partial-use, calculated" refund formula from
#14 — do not implement that formula.)

**Blocked by:** 17 (a purchase to refund), 13 (the refund/correction infra), 18 (a redeemed
credit to prove nonrefundability against)

**Status:** ready-for-agent

- [ ] A package/bundle with zero redeemed credits refunds the amount paid minus prior refunds,
      and cancels the unused entitlement, through the standard (non-manual) path
- [ ] Once any credit has been redeemed, the standard path refuses — only a manual admin/owner
      exception can proceed from here
- [ ] The manual exception requires a reason, an explicit amount, an explicit choice to keep or
      cancel remaining credits (default: cancel, on a full refund) and an explicit choice to
      preserve or reverse earned commission (default: preserve, for a goodwill refund)
- [ ] The refund cap (#13) applies here too, including through any replacement invoice lineage
- [ ] The old "paid minus (sessions used × regular price)" partial-refund formula is not
      implemented anywhere — confirm no code path computes it
