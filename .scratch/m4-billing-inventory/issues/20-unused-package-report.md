# 20 — Unused-package liability report

**What to build:** An admin-only report of every customer's unused, still-eligible package/
bundle credits, valued at their frozen purchase-time allocation, correctly accounting for
redemption, cancellation, expiry, and #19's explicit manual-refund credit-keep/cancel choice.
A goodwill refund that preserved credits must still show its remaining entitlement, not
silently drop it from the report. (#54 stories 68, "Package liability report" bullet.)

**Blocked by:** 18 (redemptions to subtract), 19 (refund/cancellation decisions to account for)

**Status:** ready-for-agent

- [ ] Report lists unused credits per customer, valued at the frozen allocation from purchase
      (#06/#17), not a live catalog price
- [ ] Redeemed credits (#18) are excluded; cancelled credits (#19's default) are excluded
- [ ] Credits explicitly *kept* by a manual refund (#19's non-default choice) still appear,
      correctly valued
- [ ] Expired credits (where expiry is enabled) are excluded once past their expiry
- [ ] Filterable at least by date range; admin/owner access only
