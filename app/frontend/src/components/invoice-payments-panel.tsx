import { CreditCard } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import type { Invoice, RetailInvoice } from '@/lib/api'

export type InvoicePaymentsPanelProps = {
  invoice: Invoice | RetailInvoice
  kind: 'service' | 'retail'
  /** Re-reads the invoice — call after a mutation this panel makes lands, so the balance
   *  breakdown above and the lineage links stay in sync with what was just recorded. */
  onRefetch: () => void
}

/**
 * Payments (#103, spec #95 stories 20-29): record a client payment, a split payment, pending
 * insurer approval and its later "mark received", a correction with a reason, and an
 * admin/owner refund or balance exception. Staff-Mode-reachable (`billing.view`, the same
 * capability viewing this invoice already required) — refunds and balance exceptions are the
 * one Admin Mode exception inside this panel, gated there once #103 builds it.
 *
 * This file is that ticket's own slot, kept out of `invoice-view.tsx` so #103 lands here
 * without touching the invoice view, the lineage cards, or any sibling action slot.
 */
export function InvoicePaymentsPanel({ invoice: _invoice, kind: _kind, onRefetch: _onRefetch }: InvoicePaymentsPanelProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CreditCard className="size-4 text-muted-foreground" aria-hidden />
          Payments
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground">
          Recording payments, insurer claims and refunds is not built yet.
        </p>
      </CardContent>
    </Card>
  )
}
