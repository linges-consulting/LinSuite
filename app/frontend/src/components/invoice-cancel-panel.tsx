import { Ban } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useCan } from '@/lib/capability-gate'
import type { Invoice, RetailInvoice } from '@/lib/api'

export type InvoiceCancelPanelProps = {
  invoice: Invoice | RetailInvoice
  kind: 'service' | 'retail'
  /** Cancelling opens a replacement draft elsewhere (bill review for a service invoice, Sell
   *  for a retail one, #95's own IA decision) rather than staying on this screen, so #107 uses
   *  this to invalidate the cached detail before it navigates away. */
  onRefetch: () => void
}

/**
 * Cancel & replace (#107, spec #95 stories 30-33): a required reason, then straight to the
 * replacement draft this invoice's own bill/sale reopens as (`replacement_bill_id`/
 * `replacement_sale_id`, `billing/invoices.py::CancelledOut`/`billing/retail_sales.py::
 * CancelledRetailOut`). Admin Mode (`billing.manage`) — #95's IA decision — rendered only
 * through that gate, and absent once an invoice is already cancelled (there is nothing left
 * to void).
 *
 * This file is that ticket's own slot, kept out of `invoice-view.tsx` so #107 lands here
 * without touching the invoice view, the lineage cards, or any sibling action slot.
 */
export function InvoiceCancelPanel({ invoice, kind: _kind, onRefetch: _onRefetch }: InvoiceCancelPanelProps) {
  const canCancel = useCan('billing.manage')
  if (!canCancel || invoice.status === 'cancelled') return null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Ban className="size-4 text-muted-foreground" aria-hidden />
          Cancel &amp; replace
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground">Cancel and replace is not built yet.</p>
      </CardContent>
    </Card>
  )
}
