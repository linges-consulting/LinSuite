import { FileText } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import type { Invoice, RetailInvoice } from '@/lib/api'

export type InvoiceDocumentsPanelProps = {
  invoice: Invoice | RetailInvoice
  kind: 'service' | 'retail'
  /** Not needed to render a print/email/receipt action today, but every action slot on this
   *  screen takes it (#102's own contract) — #104 calls it once a queued email is confirmed. */
  onRefetch: () => void
}

/**
 * Print, email and treatment receipts (#104, spec #95 stories 34-41): open the invoice PDF
 * (polling a 202 while it renders), email it to the client's address on file or a typed one
 * for a walk-in, and a per-service-line treatment receipt once checkout releases it.
 * Staff-Mode-reachable (`billing.view`), same as the rest of this screen.
 *
 * This file is that ticket's own slot, kept out of `invoice-view.tsx` so #104 lands here
 * without touching the invoice view, the lineage cards, or any sibling action slot.
 */
export function InvoiceDocumentsPanel({ invoice: _invoice, kind: _kind, onRefetch: _onRefetch }: InvoiceDocumentsPanelProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FileText className="size-4 text-muted-foreground" aria-hidden />
          Print, email &amp; receipts
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground">
          Printing, emailing and treatment receipts are not built yet.
        </p>
      </CardContent>
    </Card>
  )
}
