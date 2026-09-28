# 17 — Package/bundle purchase invoice

**What to build:** Buying a package or bundle (#06) is its own invoice, using the same
invoice-issue machinery (#11) — money, tax, numbering, payment ledger all reused, not
reimplemented for packages. Credits stay pending/inactive until the purchase is fully paid;
a partial payment never activates any entitlement. (#54 stories 52, 54.)

**Blocked by:** 06 (a package/bundle definition to purchase), 11 (the invoice machinery to
issue this purchase through)

**Status:** ready-for-agent

- [ ] Purchasing a package/bundle creates and issues an invoice through the same path as a
      service invoice — same numbering, tax, payment recording
- [ ] The purchased snapshot records the paid price, eligible services, credit quantities,
      per-session/allocated values and any expiry, frozen at purchase time
- [ ] Credits remain at zero usable entitlement until the purchase invoice is fully paid — a
      partial payment activates nothing
- [ ] The purchase itself never deducts a credit or requires an appointment
