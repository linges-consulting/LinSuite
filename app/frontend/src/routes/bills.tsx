import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Receipt } from 'lucide-react'
import { Link, useParams } from 'react-router'
import { EmptyState } from '@/components/empty-state'
import { FormError } from '@/components/form'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ApiError, applyBillDiscounts, fetchBill, fetchDraftBills, type DiscountChoice } from '@/lib/api'
import { centsToDollars } from '@/lib/money'
import { BILL, DRAFT_BILLS } from '@/lib/query-keys'

const money = (cents: number) => `$${centsToDollars(cents)}`

/**
 * Every visit with a draft service bill (#59) — completed appointments waiting to be reviewed
 * and, later, checked out. Front-desk work (`billing.view`, no Admin Mode) — `lib/nav.ts`'s
 * own gate is what decides whether the link to this screen even shows.
 */
export function BillsPage() {
  const bills = useQuery({ queryKey: DRAFT_BILLS, queryFn: fetchDraftBills })

  if (bills.isPending) return <Skeleton className="h-64 w-full" />
  if (bills.isError) {
    return (
      <p role="alert" className="text-destructive">
        {bills.error.message}
      </p>
    )
  }
  if (bills.data.length === 0) {
    return (
      <EmptyState
        icon={Receipt}
        title="No draft bills"
        description="A visit's bill shows up here once its appointment is completed."
      />
    )
  }

  return (
    <div className="rounded-xl border bg-card">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="pl-4">Client</TableHead>
            <TableHead>Lines</TableHead>
            <TableHead className="text-right">Subtotal</TableHead>
            <TableHead className="pr-4 text-right">Completed</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {bills.data.map((bill) => (
            <TableRow key={bill.id} className="h-12">
              <TableCell className="pl-4">
                <Link to={`/bills/${bill.id}`} className="font-medium hover:underline">
                  {bill.customer.name}
                </Link>
              </TableCell>
              <TableCell>{bill.line_count === 1 ? '1 line' : `${bill.line_count} lines`}</TableCell>
              <TableCell className="text-right tabular-nums">{money(bill.subtotal_cents)}</TableCell>
              <TableCell className="pr-4 text-right tabular-nums text-muted-foreground">
                {new Date(bill.created_at).toLocaleString()}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

/**
 * One visit's draft bill (#63): #59's lines, #58's discounts and #57's tax together, with the
 * total recomputed by the server on every toggle. Nothing here needs admin approval — that is
 * #64's job — so a discount applies (or lifts) the moment its checkbox changes, no separate
 * save step.
 */
export function BillReviewPage() {
  const { id = '' } = useParams()
  const queryClient = useQueryClient()
  const bill = useQuery({
    queryKey: [...BILL, id],
    queryFn: () => fetchBill(id),
    enabled: id !== '',
  })

  const apply = useMutation({
    mutationFn: (discountIds: string[]) => applyBillDiscounts(id, discountIds),
    onSuccess: (updated) => {
      queryClient.setQueryData([...BILL, id], updated)
      queryClient.invalidateQueries({ queryKey: DRAFT_BILLS })
    },
  })

  const toggleDiscount = (discountId: string, checked: boolean) => {
    if (!bill.data) return
    const selected = new Set(bill.data.eligible_discounts.filter((d) => d.applied).map((d) => d.id))
    if (checked) selected.add(discountId)
    else selected.delete(discountId)
    apply.mutate([...selected])
  }

  if (bill.isPending) return <Skeleton className="h-64 w-full" />
  if (bill.isError) {
    const message =
      bill.error instanceof ApiError && bill.error.status === 404
        ? 'No such bill.'
        : bill.error.message
    return (
      <p role="alert" className="text-destructive">
        {message}
      </p>
    )
  }

  const data = bill.data

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <Link to="/bills" className="inline-flex items-center gap-1.5 text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" aria-hidden />
        All draft bills
      </Link>

      <div>
        <h1 className="text-lg font-semibold">{data.customer.name}</h1>
        <p className="text-sm text-muted-foreground">{new Date(data.created_at).toLocaleString()}</p>
      </div>

      <div className="rounded-xl border bg-card">
        <Table>
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
            {data.lines.map((line) => (
              <TableRow key={line.id} className="h-12">
                <TableCell className="pl-4">{line.service.name}</TableCell>
                <TableCell>{line.staff.name}</TableCell>
                <TableCell className="text-right tabular-nums">{money(line.price_cents)}</TableCell>
                <TableCell className="text-right tabular-nums text-muted-foreground">
                  {line.discounted_cents < line.price_cents
                    ? `-${money(line.price_cents - line.discounted_cents)}`
                    : '—'}
                </TableCell>
                <TableCell className="text-right tabular-nums text-muted-foreground">
                  {money(line.tax.tax_cents)}
                </TableCell>
                <TableCell className="pr-4 text-right tabular-nums font-medium">
                  {money(line.line_total_cents)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Discounts</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {data.eligible_discounts.length === 0 ? (
            <p className="text-sm text-muted-foreground">No discount applies to this visit.</p>
          ) : (
            <ul className="space-y-2">
              {data.eligible_discounts.map((discount) => (
                <DiscountRow
                  key={discount.id}
                  discount={discount}
                  disabled={apply.isPending}
                  onToggle={(checked) => toggleDiscount(discount.id, checked)}
                />
              ))}
            </ul>
          )}
          {apply.isError && (
            <FormError>
              {apply.error instanceof Error ? apply.error.message : 'Could not apply these discounts'}
            </FormError>
          )}
        </CardContent>
      </Card>

      <div className="space-y-1 rounded-xl border bg-card p-4 text-sm">
        <Row label="Subtotal" value={money(data.subtotal_cents)} />
        {data.discount_total_cents > 0 && (
          <Row label="Discount" value={`-${money(data.discount_total_cents)}`} muted />
        )}
        {Object.entries(data.tax_totals_by_component).map(([code, cents]) => (
          <Row key={code} label={code} value={money(cents)} muted />
        ))}
        <div className="flex justify-between border-t pt-2 text-base font-semibold">
          <span>Total</span>
          <span className="tabular-nums">{money(data.grand_total_cents)}</span>
        </div>
      </div>
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

function DiscountRow({
  discount,
  disabled,
  onToggle,
}: {
  discount: DiscountChoice
  disabled: boolean
  onToggle: (checked: boolean) => void
}) {
  const amount =
    discount.kind === 'percentage'
      ? `${((discount.percentage_bp ?? 0) / 100).toFixed(1)}%`
      : money(discount.amount_cents ?? 0)
  return (
    <li className="flex items-center gap-2">
      <Checkbox
        id={`discount-${discount.id}`}
        checked={discount.applied}
        disabled={disabled}
        onCheckedChange={(checked) => onToggle(checked === true)}
      />
      <Label htmlFor={`discount-${discount.id}`} className="font-normal">
        {discount.name} — {amount}
        {!discount.stackable && (
          <span className="ml-1 text-xs text-muted-foreground">(not stackable with others)</span>
        )}
      </Label>
    </li>
  )
}
