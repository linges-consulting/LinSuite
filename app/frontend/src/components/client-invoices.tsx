import { useQuery } from '@tanstack/react-query'
import { Receipt } from 'lucide-react'
import { EmptyState } from '@/components/empty-state'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { fetchInvoices, fetchRetailInvoices, type InvoiceSummary, type RetailInvoiceSummary } from '@/lib/api'
import { INVOICES, RETAIL_INVOICES } from '@/lib/query-keys'
import { InvoiceRow } from '@/routes/invoices'

// A client's whole history in one card, newest first — no on-screen pagination here; #99
// only asks for that on the Billing → Invoices tab itself. One page each is plenty for a
// single client's history; the Billing tab is where years of invoices get paged.
const PAGE_SIZE = 100

/**
 * A client's Invoices tab (spec #95 user story 65): both service and retail invoices, merged
 * into one newest-first list, so their whole billing history is in one place. Behind
 * `billing.view`, the same front-desk capability as Billing itself — `clients.tsx` is what
 * decides whether this card renders. Reuses `routes/invoices.tsx`'s own `InvoiceRow`, so a
 * row here opens the same invoice view (#102) the Billing tab's row does.
 */
export function ClientInvoicesCard({ customerId }: { customerId: string }) {
  const params = { customer_id: customerId, page_size: PAGE_SIZE }
  const service = useQuery({
    queryKey: [...INVOICES, params],
    queryFn: () => fetchInvoices(params),
  })
  const retail = useQuery({
    queryKey: [...RETAIL_INVOICES, params],
    queryFn: () => fetchRetailInvoices(params),
  })
  const loading = service.isPending || retail.isPending
  const error = service.error ?? retail.error
  const rows: { row: InvoiceSummary | RetailInvoiceSummary; kind: 'service' | 'retail' }[] = [
    ...(service.data?.invoices ?? []).map((row) => ({ row, kind: 'service' as const })),
    ...(retail.data?.retail_invoices ?? []).map((row) => ({ row, kind: 'retail' as const })),
  ].sort((a, b) => b.row.issued_at.localeCompare(a.row.issued_at))

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm font-medium">Invoices</CardTitle>
      </CardHeader>
      <CardContent>
        {loading ? (
          <Skeleton className="h-32 w-full" />
        ) : error ? (
          <p role="alert" className="text-destructive">
            {error.message}
          </p>
        ) : rows.length === 0 ? (
          <EmptyState
            icon={Receipt}
            title="No invoices yet"
            description="This client's service and retail invoices will list here."
          />
        ) : (
          <div className="rounded-xl border">
            <Table aria-label="Invoices">
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-4">Number</TableHead>
                  <TableHead>Date</TableHead>
                  <TableHead>Client</TableHead>
                  <TableHead className="text-right">Total</TableHead>
                  <TableHead className="text-right">Balance</TableHead>
                  <TableHead className="pr-4">Status</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map(({ row, kind }) => (
                  <InvoiceRow key={row.id} row={row} kind={kind} />
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
