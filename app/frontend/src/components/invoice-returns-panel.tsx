import { Undo2 } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useCan } from '@/lib/capability-gate'
import type { RetailInvoice } from '@/lib/api'

export type InvoiceReturnsPanelProps = {
  invoice: RetailInvoice
  /** Re-reads the invoice once a return lands, so restocked quantities and the refund the
   *  return may carry show up in the balance breakdown above without a manual reload. */
  onRefetch: () => void
}

/**
 * Retail returns (#105, spec #95 story 50): choose lines and quantities, restock or not per
 * line, and an optional refund capped at money received. Admin Mode (`billing.manage`) — the
 * same gate #95's IA decision draws around it — rendered only through that gate (#95: "absent
 * in Staff Mode ... never disabled"), so it never appears at all for a service invoice, which
 * has no such thing.
 *
 * This file is that ticket's own slot, kept out of `invoice-view.tsx` so #105 lands here
 * without touching the invoice view, the lineage cards, or any sibling action slot.
 */
export function InvoiceReturnsPanel({ invoice: _invoice, onRefetch: _onRefetch }: InvoiceReturnsPanelProps) {
  const canReturn = useCan('billing.manage')
  if (!canReturn) return null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Undo2 className="size-4 text-muted-foreground" aria-hidden />
          Returns
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground">Processing a return is not built yet.</p>
      </CardContent>
    </Card>
  )
}
