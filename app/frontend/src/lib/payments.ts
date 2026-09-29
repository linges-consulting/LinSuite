import type { PaymentEntry } from '@/lib/api'

/**
 * The pending insurer entries no received insurer money has settled yet. The ledger settles a
 * pending row with a separate `received` row and never links the two (`billing/payments.py`:
 * "without editing it"), so this matches received insurer money against pending entries
 * oldest first — the same totals the server's `pending_insurer_cents` nets. Derived from the
 * ledger, so a reload never offers Mark received for money already recorded.
 */
export function unsettledPendingInsurerIds(
  payments: PaymentEntry[],
  supersededIds: Set<string>,
): Set<string> {
  const live = payments.filter((p) => p.payer_type === 'insurer' && !supersededIds.has(p.id))
  let received = live.filter((p) => p.status === 'received').reduce((sum, p) => sum + p.amount_cents, 0)
  const unsettled = new Set<string>()
  for (const pending of live
    .filter((p) => p.status === 'pending')
    .sort((a, b) => a.recorded_at.localeCompare(b.recorded_at))) {
    if (received >= pending.amount_cents) received -= pending.amount_cents
    else unsettled.add(pending.id)
  }
  return unsettled
}
