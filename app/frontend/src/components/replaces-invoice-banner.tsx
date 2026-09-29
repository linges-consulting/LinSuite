import { useQuery } from '@tanstack/react-query'
import { History } from 'lucide-react'
import { useLocation } from 'react-router'
import { fetchInvoice, fetchRetailInvoice, type Invoice, type RetailInvoice } from '@/lib/api'
import { INVOICE, RETAIL_INVOICE } from '@/lib/query-keys'

/** What `InvoiceCancelPanel` (#107) hands `navigate(..., { state })` right after a cancel — the
 *  original's own number and the reason typed into the cancel dialog, so the replacement draft
 *  it lands on can show the banner immediately, with no extra request (and no extra audit-log
 *  read of the original) on the one navigation that already has this in hand. */
export type ReplacesInvoiceState = { number: number; reason: string }

/**
 * "Replaces #N" (#107, spec #95 stories 31-33): shown on the replacement draft a cancel opens
 * — bill review for a service invoice, Sell for a retail one. `router state` (above) covers the
 * common case, the navigation straight off a cancel; `replacesInvoiceId` — `Bill.
 * replaces_invoice_id` / `RetailSale.replaces_retail_invoice_id`, both #107's own backend slot
 * — is the fallback read for a reload or a direct link, using the same `INVOICE`/
 * `RETAIL_INVOICE` cache and endpoints the invoice view itself reads by.
 */
export function ReplacesInvoiceBanner({
  replacesInvoiceId,
  kind,
}: {
  replacesInvoiceId: string | null
  kind: 'service' | 'retail'
}) {
  const location = useLocation()
  const fromNavigation = (location.state as { replacesInvoice?: ReplacesInvoiceState } | null)
    ?.replacesInvoice

  const query = useQuery<Invoice | RetailInvoice>({
    queryKey: kind === 'retail' ? [...RETAIL_INVOICE, replacesInvoiceId] : [...INVOICE, replacesInvoiceId],
    queryFn: (): Promise<Invoice | RetailInvoice> =>
      kind === 'retail' ? fetchRetailInvoice(replacesInvoiceId ?? '') : fetchInvoice(replacesInvoiceId ?? ''),
    enabled: replacesInvoiceId !== null && !fromNavigation,
  })

  if (!replacesInvoiceId) return null
  const banner =
    fromNavigation ??
    (query.data ? { number: query.data.invoice_number, reason: query.data.cancel_reason ?? '' } : null)
  if (!banner) return null

  return (
    <div className="flex items-start gap-2 rounded-lg border border-primary/30 bg-primary/5 p-3 text-sm">
      <History className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden />
      <div>
        <p className="font-medium">Replaces #{banner.number}</p>
        {banner.reason && <p className="text-muted-foreground">{banner.reason}</p>}
      </div>
    </div>
  )
}
