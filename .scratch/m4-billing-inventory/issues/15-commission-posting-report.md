# 15 — Commission posting + report

**What to build:** Commission posts when the reviewed bill is finalized (invoice issue), using
the rate already snapshotted at appointment completion (#05) against the final approved
pre-tax basis. An admin-only report shows earned service/retail commission separately from
cash received/pending, filterable by date range and staff, exportable as CSV. Rates and amounts
never leak into any staff-facing payload. (#54 stories 94, 97-100.)

**Blocked by:** 11 (an issued invoice to post from), 05 (the rate snapshot to post with)

**Status:** ready-for-agent

- [ ] Commission is calculated and posted at invoice issue, using the rate captured at
      completion (#05) and the final discounted, business-vs-commission-basis-aware charge
      (#04's per-discount choice)
- [ ] Package-service commission credits the practitioner who delivered the session, not
      whoever sold the package
- [ ] The admin report shows earned commission separate from received/pending payments,
      filterable by date range and staff, with CSV export
- [ ] No staff-facing bill, payment, roster or document payload includes commission rates or
      amounts — verified by checking those response shapes directly, not just the report
      endpoint's own access gate
- [ ] Refunds/replacements (once #13/#14 exist) post as dated reversing entries in the period
      they occur, not by rewriting the original posting
