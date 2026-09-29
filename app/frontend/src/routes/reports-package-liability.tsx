import { useQuery } from '@tanstack/react-query'
import { BarChart3 } from 'lucide-react'
import { useState } from 'react'
import { EmptyState } from '@/components/empty-state'
import { ExportControl } from '@/components/export-control'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import {
  downloadPackageLiabilityExport,
  fetchPackageLiabilityExportStatus,
  fetchPackageLiabilityReport,
  requestPackageLiabilityExport,
  searchCustomers,
  type Customer,
} from '@/lib/api'
import { centsToDollars } from '@/lib/money'
import { CUSTOMERS, PACKAGE_LIABILITY_REPORT } from '@/lib/query-keys'

const money = (cents: number) => `$${centsToDollars(cents)}`

/** `5 Jan 2026`: a purchase or expiry date needs no clock. */
function shortDate(instant: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date(instant))
}

/**
 * Package liability (spec #95 user story 68, #110): a client filter, outstanding package
 * credits and their value, CSV export via the shared `ExportControl`. `billing.manage`,
 * Admin Mode — `routes/reports.tsx` is what decides whether this tab is even offered.
 */
export function PackageLiabilityReportTab() {
  const [customer, setCustomer] = useState<Customer | null>(null)
  const [search, setSearch] = useState('')

  const matches = useQuery({
    queryKey: [...CUSTOMERS, search],
    queryFn: () => searchCustomers(search),
    enabled: !customer && search.trim().length > 0,
  })
  const report = useQuery({
    queryKey: [...PACKAGE_LIABILITY_REPORT, customer?.id],
    queryFn: () => fetchPackageLiabilityReport({ customer_id: customer?.id }),
  })

  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-end justify-between gap-3">
        <CardTitle className="text-base font-medium">
          <h2>Package liability</h2>
        </CardTitle>
        <div className="flex flex-wrap items-end gap-2">
          <div className="grid gap-1">
            <Label htmlFor="liability-client" className="text-xs text-muted-foreground">
              Client
            </Label>
            {customer ? (
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">
                  {customer.first_name} {customer.last_name}
                </span>
                <Button type="button" variant="ghost" size="sm" onClick={() => setCustomer(null)}>
                  Clear
                </Button>
              </div>
            ) : (
              <Input
                id="liability-client"
                className="w-56"
                placeholder="All clients"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            )}
          </div>
          <ExportControl
            requestExport={() => requestPackageLiabilityExport({ customer_id: customer?.id })}
            pollExport={fetchPackageLiabilityExportStatus}
            downloadExport={downloadPackageLiabilityExport}
          />
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        {!customer && matches.data && matches.data.length > 0 && (
          <ul className="flex flex-col divide-y rounded-lg border">
            {matches.data.map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm hover:bg-accent"
                  onClick={() => {
                    setCustomer(c)
                    setSearch('')
                  }}
                >
                  <span className="font-medium">
                    {c.first_name} {c.last_name}
                  </span>
                  <span className="tabular-nums text-muted-foreground">{c.email ?? ''}</span>
                </button>
              </li>
            ))}
          </ul>
        )}

        {report.isPending ? (
          <Skeleton className="h-40 w-full" />
        ) : report.isError ? (
          <p role="alert" className="text-destructive">
            Could not load the package-liability report.
          </p>
        ) : report.data.rows.length === 0 ? (
          <EmptyState
            icon={BarChart3}
            title="No outstanding package credits"
            description="Nobody has unused package credits, for this filter."
          />
        ) : (
          <>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Client</TableHead>
                  <TableHead className="text-right">Credits remaining</TableHead>
                  <TableHead className="text-right">Unused value</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {report.data.customers.map((c) => (
                  <TableRow key={c.customer_id}>
                    <TableCell className="font-medium">{c.customer_name}</TableCell>
                    <TableCell className="text-right tabular-nums">{c.credits_remaining}</TableCell>
                    <TableCell className="text-right tabular-nums">{money(c.unused_value_cents)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>

            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Client</TableHead>
                  <TableHead>Package</TableHead>
                  <TableHead>Service</TableHead>
                  <TableHead>Purchased</TableHead>
                  <TableHead>Expires</TableHead>
                  <TableHead className="text-right">Remaining</TableHead>
                  <TableHead className="text-right">Value</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {report.data.rows.map((row) => (
                  <TableRow key={`${row.package_purchase_id}-${row.service_id}`}>
                    <TableCell>{row.customer_name}</TableCell>
                    <TableCell>{row.package_name}</TableCell>
                    <TableCell>{row.service_name}</TableCell>
                    <TableCell>{shortDate(row.purchased_at)}</TableCell>
                    <TableCell>{row.expires_at ? shortDate(row.expires_at) : 'Never'}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {row.credits_remaining} / {row.credits_total}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{money(row.unused_value_cents)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </>
        )}
      </CardContent>
    </Card>
  )
}
