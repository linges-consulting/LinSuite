import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, Receipt } from 'lucide-react'
import { useDeferredValue, useState } from 'react'
import { Link } from 'react-router'
import { EmptyState } from '@/components/empty-state'
import { InvoiceStatusBadge } from '@/components/invoice-status-badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import {
  fetchInvoices,
  fetchRetailInvoices,
  searchCustomers,
  type Customer,
  type InvoiceListStatus,
  type InvoiceSummary,
  type RetailInvoiceSummary,
} from '@/lib/api'
import { centsToDollars } from '@/lib/money'
import { INVOICES, RETAIL_INVOICES } from '@/lib/query-keys'

const PAGE_SIZE = 25
const money = (cents: number) => `$${centsToDollars(cents)}`

type Kind = 'service' | 'retail'

/**
 * Billing → Invoices (#99, spec #95 stories 6-14): a Service | Retail switch, date range,
 * status and client filters, a paginated table that opens the invoice view (#102) from a row.
 * Service and retail stay two lists, never merged (CLAUDE.md: "Services and retail invoice
 * separately") — each keeps its own numbering, its own query, and its own page.
 *
 * A retail row links with `?kind=retail` on the shared `/bills/invoices/:id` route (a service
 * row links there plain) — the one bit #102's invoice view needs to know which table to read
 * the id against, since the two ids are drawn from separate UUID spaces with no shared prefix.
 */
export function InvoicesTab() {
  const [kind, setKind] = useState<Kind>('service')
  const [range, setRange] = useState<{ from?: string; to?: string }>({})
  const [status, setStatus] = useState<InvoiceListStatus | 'all'>('all')
  const [customer, setCustomer] = useState<Customer | null>(null)
  const [page, setPage] = useState(1)

  const changeKind = (next: Kind) => {
    setKind(next)
    setPage(1)
  }
  const choose = (next: { from?: string; to?: string }) => {
    setRange((r) => ({ from: r.from, to: r.to, ...next }))
    setPage(1)
  }
  const chooseStatus = (next: InvoiceListStatus | 'all') => {
    setStatus(next)
    setPage(1)
  }
  const chooseCustomer = (next: Customer | null) => {
    setCustomer(next)
    setPage(1)
  }

  const params = {
    customer_id: customer?.id,
    from: range.from,
    to: range.to,
    status: status === 'all' ? undefined : status,
    page,
    page_size: PAGE_SIZE,
  }
  const serviceList = useQuery({
    queryKey: [...INVOICES, params],
    queryFn: () => fetchInvoices(params),
    enabled: kind === 'service',
    placeholderData: keepPreviousData,
  })
  const retailList = useQuery({
    queryKey: [...RETAIL_INVOICES, params],
    queryFn: () => fetchRetailInvoices(params),
    enabled: kind === 'retail',
    placeholderData: keepPreviousData,
  })
  const active = kind === 'service' ? serviceList : retailList
  const rows: (InvoiceSummary | RetailInvoiceSummary)[] =
    kind === 'service' ? (serviceList.data?.invoices ?? []) : (retailList.data?.retail_invoices ?? [])
  const total = active.data?.total ?? 0
  const from = range.from ?? active.data?.from ?? ''
  const to = range.to ?? active.data?.to ?? ''
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const first = (page - 1) * PAGE_SIZE + 1

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex gap-1">
          <Button
            type="button"
            size="sm"
            variant={kind === 'service' ? 'default' : 'outline'}
            aria-pressed={kind === 'service'}
            onClick={() => changeKind('service')}
          >
            Service
          </Button>
          <Button
            type="button"
            size="sm"
            variant={kind === 'retail' ? 'default' : 'outline'}
            aria-pressed={kind === 'retail'}
            onClick={() => changeKind('retail')}
          >
            Retail
          </Button>
        </div>
        <div className="grid gap-1">
          <Label htmlFor="invoices-from" className="text-xs text-muted-foreground">
            From
          </Label>
          <Input
            id="invoices-from"
            type="date"
            className="w-40"
            value={from}
            max={to || undefined}
            onChange={(e) => choose({ from: e.target.value })}
          />
        </div>
        <div className="grid gap-1">
          <Label htmlFor="invoices-to" className="text-xs text-muted-foreground">
            To
          </Label>
          <Input
            id="invoices-to"
            type="date"
            className="w-40"
            value={to}
            min={from || undefined}
            onChange={(e) => choose({ to: e.target.value })}
          />
        </div>
        <div className="grid gap-1">
          <Label htmlFor="invoices-status" className="text-xs text-muted-foreground">
            Status
          </Label>
          <Select
            value={status}
            onValueChange={(v) => chooseStatus(v as InvoiceListStatus | 'all')}
          >
            <SelectTrigger id="invoices-status" className="w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All statuses</SelectItem>
              <SelectItem value="outstanding">Outstanding</SelectItem>
              <SelectItem value="paid">Paid</SelectItem>
              <SelectItem value="cancelled">Cancelled</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <ClientFilter customer={customer} onChange={chooseCustomer} />
        {active.data && (
          <p className="ml-auto text-muted-foreground tabular-nums" aria-live="polite">
            {total === 1 ? '1 invoice' : `${total} invoices`}
          </p>
        )}
      </div>

      {active.isPending ? (
        <Skeleton className="h-64 w-full" />
      ) : active.isError ? (
        <p role="alert" className="text-destructive">
          {active.error.message}
        </p>
      ) : rows.length === 0 ? (
        <EmptyState
          icon={Receipt}
          title="No invoices in this range"
          description="Try a wider date range, or clear a filter."
        />
      ) : (
        <div
          className={`rounded-xl border bg-card transition-opacity ${active.isPlaceholderData ? 'opacity-60' : ''}`}
          aria-busy={active.isPlaceholderData}
        >
          <Table aria-label={kind === 'service' ? 'Service invoices' : 'Retail invoices'}>
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
              {rows.map((row) => (
                <InvoiceRow key={row.id} row={row} kind={kind} />
              ))}
            </TableBody>
          </Table>
          {pages > 1 && (
            <div className="flex items-center justify-between border-t px-4 py-2 text-muted-foreground">
              <span className="tabular-nums">
                {first}–{Math.min(page * PAGE_SIZE, total)} of {total}
              </span>
              <div className="flex gap-1">
                <Button
                  variant="outline"
                  size="icon-sm"
                  aria-label="Previous page"
                  disabled={page <= 1}
                  onClick={() => setPage((p) => p - 1)}
                >
                  <ChevronLeft />
                </Button>
                <Button
                  variant="outline"
                  size="icon-sm"
                  aria-label="Next page"
                  disabled={page >= pages}
                  onClick={() => setPage((p) => p + 1)}
                >
                  <ChevronRight />
                </Button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function InvoiceRow({ row, kind }: { row: InvoiceSummary | RetailInvoiceSummary; kind: Kind }) {
  const to = kind === 'retail' ? `/bills/invoices/${row.id}?kind=retail` : `/bills/invoices/${row.id}`
  return (
    <TableRow className="h-10">
      <TableCell className="pl-4 font-medium tabular-nums">
        <Link to={to} className="hover:underline">
          #{row.invoice_number}
        </Link>
      </TableCell>
      <TableCell className="tabular-nums">
        <time dateTime={row.issued_at}>{shortDate(row.issued_at)}</time>
      </TableCell>
      <TableCell>
        {row.customer_name ?? <span className="text-muted-foreground">Walk-in</span>}
      </TableCell>
      <TableCell className="text-right tabular-nums">{money(row.grand_total_cents)}</TableCell>
      <TableCell className="text-right tabular-nums">{money(row.outstanding_cents)}</TableCell>
      <TableCell className="pr-4">
        <InvoiceStatusBadge status={row.list_status} />
      </TableCell>
    </TableRow>
  )
}

function shortDate(instant: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date(instant))
}

/**
 * A small inline client search — the same "search then pick from a list" shape
 * `routes/schedule.tsx`'s booking dialog uses for "Existing client", scoped here to a
 * chosen-or-not chip rather than a form field.
 */
export function ClientFilter({
  customer,
  onChange,
}: {
  customer: Customer | null
  onChange: (c: Customer | null) => void
}) {
  const [search, setSearch] = useState('')
  const term = useDeferredValue(search.trim())
  const matches = useQuery({
    queryKey: ['invoice-client-filter', term],
    queryFn: () => searchCustomers(term),
    enabled: term.length > 0 && !customer,
  })
  if (customer) {
    return (
      <div className="flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-sm">
        <span className="font-medium">
          {customer.first_name} {customer.last_name}
        </span>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-6 px-1.5"
          onClick={() => onChange(null)}
        >
          Clear
        </Button>
      </div>
    )
  }
  return (
    <div className="relative grid gap-1">
      <Label htmlFor="invoices-client" className="text-xs text-muted-foreground">
        Client
      </Label>
      <Input
        id="invoices-client"
        className="w-48"
        placeholder="Name, phone or email"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      {matches.data && matches.data.length > 0 && (
        <ul className="absolute top-full z-10 mt-1 flex w-64 flex-col divide-y rounded-lg border bg-popover shadow-md">
          {matches.data.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                className="w-full px-3 py-1.5 text-left text-sm hover:bg-accent"
                onClick={() => {
                  onChange(c)
                  setSearch('')
                }}
              >
                {c.first_name} {c.last_name}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
