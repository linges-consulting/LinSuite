# 08 — Low-stock alerts

**What to build:** In-app low-stock warnings and an opt-in email alert (via M3's verified
notification sender) when a variant's stock crosses below its own threshold. Exactly one
warning per crossing — no repeat while still below threshold — and restocking above the
threshold rearms the next crossing. Evaluated transactionally with the stock write; delivery
is queued after commit, deduplicated against retries. (#54 stories 83-86.)

**Blocked by:** 07 (needs the movement ledger to detect a crossing against)

**Status:** ready-for-agent

- [ ] Each variant tracks whether it is currently "armed" (above threshold) or "already
      alerted" (below threshold, warning already sent) — the crossing check is part of the
      same transaction as the stock-changing write
- [ ] A sale that takes stock from above to below the threshold triggers exactly one alert;
      a second sale that keeps it below triggers none
- [ ] Restocking back above the threshold rearms it; the next crossing below alerts again
- [ ] In-app warning is always shown regardless of email opt-in; email is admin/owner opt-in,
      queued through Celery, never sent inline
- [ ] Two concurrent sales crossing the threshold at once produce exactly one alert, not two
- [ ] An unverified/unconfigured sender degrades the same way M3's other notifications do —
      no crash, no silent double-send on retry
