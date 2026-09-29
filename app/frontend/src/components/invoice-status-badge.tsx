import { Badge } from '@/components/ui/badge'
import type { InvoiceListStatus } from '@/lib/api'

/**
 * The Invoices list's status badge (#99): `outstanding | paid | cancelled`, exactly the value
 * the server derived from the invoice's own balance (`billing/payments.py::
 * invoice_list_status`) — this component never re-derives it, only labels it. Shared by the
 * Billing → Invoices tab and a client's Invoices tab so the two never grow different colours
 * for the same status.
 */
const STATUS: Record<InvoiceListStatus, { label: string; variant: 'warning' | 'success' | 'outline' }> = {
  outstanding: { label: 'Outstanding', variant: 'warning' },
  paid: { label: 'Paid', variant: 'success' },
  cancelled: { label: 'Cancelled', variant: 'outline' },
}

export function InvoiceStatusBadge({ status }: { status: InvoiceListStatus }) {
  const { label, variant } = STATUS[status]
  return <Badge variant={variant}>{label}</Badge>
}
