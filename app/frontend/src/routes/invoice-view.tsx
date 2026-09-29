import { useQuery } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { InvoiceCancelPanel } from '@/components/invoice-cancel-panel'
import { InvoiceDocumentsPanel } from '@/components/invoice-documents-panel'
import { InvoicePaymentsPanel } from '@/components/invoice-payments-panel'
import { InvoiceReturnsPanel } from '@/components/invoice-returns-panel'
import { InvoiceStatusBadge } from '@/components/invoice-status-badge'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import {
  ApiError,
  fetchInvoice,
  fetchRetailInvoice,
  type Invoice,
  type InvoiceLine,
  type InvoiceListBalance,
  type InvoiceListStatus,
  type RetailInvoice,
  type RetailInvoiceLine,
} from '@/lib/api'
import { centsToDollars } from '@/lib/money'
import { INVOICE, RETAIL_INVOICE } from '@/lib/query-keys'

const money = (cents: number) => `$${centsToDollars(cents)}`

/**
 * One invoice (spec #95 user stories 15-41; #102): lines with discounts and per-line tax, the
 * override adjustment when present, the full balance breakdown and replacement lineage both
 * ways — for both a service invoice (`?kind` unset) and a retail one (`?kind=retail`, set by
 * #99's Invoices tab row link, since the two ids are drawn from separate UUID spaces with no
 * shared prefix). Payments (#103), print/email/receipts (#104), returns (#105, retail only)
 * and cancel & replace (#107) are each their own file under `components/invoice-*-panel.tsx`
 * — named slots here, so a later ticket edits its own file rather than this one.
 */
export function InvoiceViewPage() {
  const { id = '' } = useParams()
  const [params] = useSearchParams()
  return params.get('kind') === 'retail' ? (
    <RetailInvoiceScreen id={id} />
  ) : (
    <ServiceInvoiceScreen id={id} />
  )
}

function ServiceInvoiceScreen({ id }: { id: string }) {
  const query = useQuery({
    queryKey: [...INVOICE, id],
    queryFn: () => fetchInvoice(id),
    enabled: id !== '',
  })
  if (query.isPending) return <Skeleton className="h-64 w-full" />
  if (query.isError) return <InvoiceLoadError error={query.error} />

  const invoice = query.data
  return (
    <InvoiceShell
      invoice={invoice}
      kind="service"
      onRefetch={() => query.refetch()}
      lines={<ServiceLinesTable lines={invoice.lines} />}
      extra={
        invoice.override_applied_cents !== null && (
          <OverrideCard
            appliedCents={invoice.override_applied_cents}
            reason={invoice.override_reason}
          />
        )
      }
    />
  )
}

function RetailInvoiceScreen({ id }: { id: string }) {
  const query = useQuery({
    queryKey: [...RETAIL_INVOICE, id],
    queryFn: () => fetchRetailInvoice(id),
    enabled: id !== '',
  })
  if (query.isPending) return <Skeleton className="h-64 w-full" />
  if (query.isError) return <InvoiceLoadError error={query.error} />

  const invoice = query.data
  return (
    <InvoiceShell
      invoice={invoice}
      kind="retail"
      onRefetch={() => query.refetch()}
      lines={<RetailLinesTable lines={invoice.lines} />}
    />
  )
}

function InvoiceLoadError({ error }: { error: Error }) {
  const message = error instanceof ApiError && error.status === 404 ? 'No such invoice.' : error.message
  return (
    <p role="alert" className="text-destructive">
      {message}
    </p>
  )
}

/** The fields the shell needs — every one of them common to `Invoice` and `RetailInvoice`
 *  (structurally: `Invoice.customer_id`/`customer_name` are plain strings, which satisfy the
 *  `| null` here, since retail's own anonymous walk-in is the only case that is ever null). */
type ShellInvoice = InvoiceListBalance & {
  id: string
  invoice_number: number
  customer_id: string | null
  customer_name: string | null
  status: 'issued' | 'cancelled'
  grand_total_cents: number
  issued_at: string
  replaces_invoice_id: string | null
  replaced_by_invoice_id: string | null
  cancelled_at: string | null
  cancel_reason: string | null
}

/** The list badge (#99) derived the same way the server derives it (`billing/payments.py::
 *  invoice_list_status`) — never a stored value on the detail response, but the same rule. */
function deriveListStatus(invoice: ShellInvoice): InvoiceListStatus {
  if (invoice.status === 'cancelled') return 'cancelled'
  return invoice.outstanding_cents > 0 ? 'outstanding' : 'paid'
}

function InvoiceShell({
  invoice,
  kind,
  onRefetch,
  lines,
  extra,
}: {
  invoice: Invoice | RetailInvoice
  kind: 'service' | 'retail'
  onRefetch: () => void
  lines: ReactNode
  extra?: ReactNode
}) {
  const customerLabel = invoice.customer_name ?? (kind === 'retail' ? 'Walk-in' : '—')

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <Link
        to="/bills"
        className="inline-flex items-center gap-1.5 text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-4" aria-hidden />
        All invoices
      </Link>

      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-lg font-semibold">
            {kind === 'retail' ? 'Retail invoice' : 'Invoice'} #{invoice.invoice_number}
          </h1>
          <p className="text-sm text-muted-foreground">
            {customerLabel} · <time dateTime={invoice.issued_at}>{formatDate(invoice.issued_at)}</time>
          </p>
        </div>
        <InvoiceStatusBadge status={deriveListStatus(invoice)} />
      </div>

      {invoice.status === 'cancelled' && (
        <Card className="border-destructive/30">
          <CardContent className="pt-4 text-sm">
            <p className="font-medium">Cancelled{invoice.cancelled_at && ` — ${formatDate(invoice.cancelled_at)}`}</p>
            {invoice.cancel_reason && <p className="text-muted-foreground">{invoice.cancel_reason}</p>}
          </CardContent>
        </Card>
      )}

      <LineageCard invoice={invoice} kind={kind} />

      {lines}

      {extra}

      <BalanceCard invoice={invoice} />

      <InvoicePaymentsPanel invoice={invoice} kind={kind} onRefetch={onRefetch} />
      <InvoiceDocumentsPanel invoice={invoice} kind={kind} onRefetch={onRefetch} />
      {kind === 'retail' && (
        <InvoiceReturnsPanel invoice={invoice as RetailInvoice} onRefetch={onRefetch} />
      )}
      <InvoiceCancelPanel invoice={invoice} kind={kind} onRefetch={onRefetch} />
    </div>
  )
}

/** Replacement lineage, both ways (#68/#102 story 18): a cancelled invoice links to what
 *  replaced it, a replacement links to what it replaces. Only the id is on the wire, so the
 *  link reads by relationship rather than by the other invoice's own number. */
function LineageCard({ invoice, kind }: { invoice: ShellInvoice; kind: 'service' | 'retail' }) {
  if (!invoice.replaces_invoice_id && !invoice.replaced_by_invoice_id) return null
  const suffix = kind === 'retail' ? '?kind=retail' : ''
  return (
    <div className="flex flex-col gap-1 rounded-lg border bg-muted/40 p-3 text-sm">
      {invoice.replaces_invoice_id && (
        <Link
          to={`/bills/invoices/${invoice.replaces_invoice_id}${suffix}`}
          className="text-primary hover:underline"
        >
          Replaces a cancelled invoice — view the original
        </Link>
      )}
      {invoice.replaced_by_invoice_id && (
        <Link
          to={`/bills/invoices/${invoice.replaced_by_invoice_id}${suffix}`}
          className="text-primary hover:underline"
        >
          Replaced by a later invoice — view the replacement
        </Link>
      )}
    </div>
  )
}

function BalanceCard({ invoice }: { invoice: ShellInvoice }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Balance</CardTitle>
      </CardHeader>
      <CardContent className="space-y-1 text-sm">
        <Row label="Total" value={money(invoice.grand_total_cents)} />
        {invoice.prepaid_cents > 0 && <Row label="Prepaid (package credit)" value={money(invoice.prepaid_cents)} muted />}
        <Row label="Outstanding" value={money(invoice.outstanding_cents)} />
        {invoice.pending_insurer_cents > 0 && (
          <Row label="Pending insurer" value={money(invoice.pending_insurer_cents)} muted />
        )}
        {invoice.client_outstanding_cents !== invoice.outstanding_cents && (
          <Row label="Client's share" value={money(invoice.client_outstanding_cents)} muted />
        )}
        {invoice.refunded_cents > 0 && <Row label="Refunded" value={money(invoice.refunded_cents)} muted />}
        {invoice.held_credit_cents > 0 && (
          <Row label="Held credit" value={money(invoice.held_credit_cents)} muted />
        )}
      </CardContent>
    </Card>
  )
}

function OverrideCard({ appliedCents, reason }: { appliedCents: number; reason: string | null }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Admin-authorized adjustment</CardTitle>
      </CardHeader>
      <CardContent className="space-y-1 text-sm">
        <Row label="Billed total" value={money(appliedCents)} />
        {reason && <p className="text-muted-foreground">{reason}</p>}
      </CardContent>
    </Card>
  )
}

function ServiceLinesTable({ lines }: { lines: InvoiceLine[] }) {
  return (
    <div className="rounded-xl border bg-card">
      <Table aria-label="Invoice lines">
        <TableHeader>
          <TableRow>
            <TableHead className="pl-4">Service</TableHead>
            <TableHead>Staff</TableHead>
            <TableHead className="text-right">Price</TableHead>
            <TableHead className="text-right">Discount</TableHead>
            <TableHead className="text-right">Tax</TableHead>
            <TableHead className="pr-4 text-right">Total</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {lines.map((line) => {
            const discountCents = line.price_cents - line.discounted_cents
            return (
              <TableRow key={line.id} className="h-12">
                <TableCell className="pl-4">
                  <span className="inline-flex items-center gap-2">
                    {line.service_name}
                    {line.prepaid_cents > 0 && <Badge variant="info">Prepaid</Badge>}
                  </span>
                  {line.discounts.length > 0 && (
                    <p className="text-xs text-muted-foreground">
                      {line.discounts.map((d) => d.discount_name).join(', ')}
                    </p>
                  )}
                  {line.override_adjustment_cents !== 0 && (
                    <p className="text-xs text-muted-foreground">
                      Admin adjustment: {line.override_adjustment_cents > 0 ? '+' : ''}
                      {money(line.override_adjustment_cents)}
                    </p>
                  )}
                </TableCell>
                <TableCell>{line.staff_name}</TableCell>
                <TableCell className="text-right tabular-nums">{money(line.price_cents)}</TableCell>
                <TableCell className="text-right tabular-nums text-muted-foreground">
                  {discountCents > 0 ? `-${money(discountCents)}` : '—'}
                </TableCell>
                <TableCell className="text-right tabular-nums text-muted-foreground">
                  {money(line.tax_cents)}
                </TableCell>
                <TableCell className="pr-4 text-right tabular-nums font-medium">
                  {money(line.line_total_cents)}
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

function RetailLinesTable({ lines }: { lines: RetailInvoiceLine[] }) {
  return (
    <div className="rounded-xl border bg-card">
      <Table aria-label="Invoice lines">
        <TableHeader>
          <TableRow>
            <TableHead className="pl-4">Product</TableHead>
            <TableHead className="text-right">Qty</TableHead>
            <TableHead className="text-right">Price</TableHead>
            <TableHead className="text-right">Discount</TableHead>
            <TableHead className="text-right">Tax</TableHead>
            <TableHead className="pr-4 text-right">Total</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {lines.map((line) => (
            <TableRow key={line.id} className="h-12">
              <TableCell className="pl-4">
                {line.variant_name}
                {line.discounts.length > 0 && (
                  <p className="text-xs text-muted-foreground">
                    {line.discounts.map((d) => d.discount_name).join(', ')}
                  </p>
                )}
              </TableCell>
              <TableCell className="text-right tabular-nums">{line.quantity}</TableCell>
              <TableCell className="text-right tabular-nums">{money(line.unit_price_cents)}</TableCell>
              <TableCell className="text-right tabular-nums text-muted-foreground">
                {line.discount_cents > 0 ? `-${money(line.discount_cents)}` : '—'}
              </TableCell>
              <TableCell className="text-right tabular-nums text-muted-foreground">
                {money(line.tax_cents)}
              </TableCell>
              <TableCell className="pr-4 text-right tabular-nums font-medium">
                {money(line.line_total_cents)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

function Row({ label, value, muted }: { label: string; value: string; muted?: boolean }) {
  return (
    <div className={`flex justify-between ${muted ? 'text-muted-foreground' : ''}`}>
      <span>{label}</span>
      <span className="tabular-nums">{value}</span>
    </div>
  )
}

function formatDate(instant: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(
    new Date(instant),
  )
}
