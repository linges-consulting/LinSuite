# 06 — Package & bundle definitions

**What to build:** Admin-defined prepaid packages (credits for one service) and bundles
(credits for several explicitly eligible services), with credit quantities, per-session values,
proportional value allocation for bundles (frozen at purchase time, not recomputed later),
optional expiry (disabled by default) and non-transferability by default. Definitions only —
no purchase flow yet (#17). (#54 stories 52, 53, 58, 66, 67.)

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] Admin can define a package (one eligible service, N credits) or a bundle (several named
      eligible services, one credit each or as configured)
- [ ] A pure function allocates a bundle's purchase price proportionally to the *regular*
      prices of its constituent services, with deterministic cent-remainder handling so the
      allocated values always sum exactly to the purchase price (worked example: 120/60/60
      regular prices, 200 purchase price → 100/50/50)
- [ ] Expiry is off by default; enabling it is an explicit per-definition choice
- [ ] Transfer between customers is disabled by default at the definition level
- [ ] No mixed service/product package and no retail credit bundle can be defined (out of
      scope, per #54) — the schema should not make this representable
