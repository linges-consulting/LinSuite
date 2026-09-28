# 02 — Product & variant catalog

**What to build:** An admin-facing catalog of retail products, each with one or more variants
(size, colour, etc.), carrying SKU, barcode, price, tax settings and a whole-unit stock count
with its own per-variant low-stock threshold. This is the foundation #07/#08/#21/#22 build on;
no stock *movements* yet — just the catalog shape and starting counts. (#54 stories 69, 70, 83.)

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] Admin can create/edit a product with one or more variants, each carrying its own SKU,
      barcode, price and stock count
- [ ] Stock is a whole-unit integer; the schema rejects fractional or negative quantities
- [ ] Each variant carries its own low-stock threshold, independent of siblings
- [ ] Products/variants can be deactivated (not hard-deleted) without breaking historical
      references from invoices already issued
- [ ] `catalog.manage`-equivalent capability gates writes; reading the catalog is available to
      whichever capability retail checkout will need (coordinate the exact key with #21)
