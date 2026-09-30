import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Receipt, ShieldCheck } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { EmptyState } from '@/components/empty-state'
import { Field, Form, FormError } from '@/components/form'
import { ReplacesInvoiceBanner } from '@/components/replaces-invoice-banner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import {
  ApiError,
  type Bill,
  type DiscountChoice,
  type OverrideRequest,
  applyBillDiscounts,
  applyInlineAdminEdit,
  authenticateInlineAdmin,
  decideOverrideRequest,
  fetchBill,
  fetchDraftBills,
  fetchInlineAdminStatus,
  fetchOverrideRequests,
  issueBill,
  releaseInlineAdmin,
  requestBillOverride,
} from '@/lib/api'
import { useCan } from '@/lib/capability-gate'
import { dollarsToCents, money } from '@/lib/money'
import { BILL, DRAFT_BILLS, INLINE_ADMIN, OVERRIDE_REQUESTS } from '@/lib/query-keys'


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
  const navigate = useNavigate()
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

  // #102: issuing turns this reviewed draft into a real invoice and lands there — the one
  // place a bill stops being reviewed and starts being billed.
  const issue = useMutation({
    mutationFn: () => issueBill(id),
    onSuccess: (invoice) => {
      queryClient.invalidateQueries({ queryKey: DRAFT_BILLS })
      navigate(`/bills/invoices/${invoice.id}`)
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

      <ReplacesInvoiceBanner replacesInvoiceId={data.replaces_invoice_id} kind="service" />

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
                <TableCell className="pl-4">
                  <span className="inline-flex items-center gap-2">
                    {line.service.name}
                    {line.prepaid_cents > 0 && <Badge variant="info">Prepaid</Badge>}
                  </span>
                </TableCell>
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
          <span>{data.override_total_cents !== null ? 'Computed total' : 'Total'}</span>
          <span className="tabular-nums">{money(data.grand_total_cents)}</span>
        </div>
        {data.override_total_cents !== null && (
          <div className="flex justify-between pt-1 text-base font-semibold text-primary">
            <span>Admin-authorized total</span>
            <span className="tabular-nums">{money(data.override_total_cents)}</span>
          </div>
        )}
        {data.override_reason && (
          <p className="pt-1 text-xs text-muted-foreground">Reason: {data.override_reason}</p>
        )}
      </div>

      {(data.bill_override_requests_enabled || data.inline_admin_bill_edit_enabled) && (
        <BillAuthorityCard billId={id} bill={data} />
      )}

      {data.status === 'draft' && (
        <div className="flex flex-col items-end gap-2">
          {issue.isError && (
            <FormError>
              {issue.error instanceof Error ? issue.error.message : 'Could not issue this invoice'}
            </FormError>
          )}
          <Button onClick={() => issue.mutate()} disabled={issue.isPending}>
            Issue invoice
          </Button>
        </div>
      )}
    </div>
  )
}

/**
 * Bill review authority (#64): the two override paths on top of everything above. A staff
 * member submits an ad hoc discount or price override for admin/owner review
 * (`enable_bill_override_requests`), or an admin/owner signs in with their own credentials
 * right here to make a supervised edit under their own identity
 * (`enable_inline_admin_bill_edit`) — never merging into the staff account's own permissions.
 * Both sections are individually hidden when their toggle is off; the server enforces the
 * same gate on a direct call, this is only the convenience of not offering a disabled button.
 */
function BillAuthorityCard({ billId, bill }: { billId: string; bill: Bill }) {
  const queryClient = useQueryClient()
  // Administrative (#114, spec #113 Staff Mode section): the buttons below are absent, not
  // merely disabled, for a session holding `billing.manage` while it is in Staff Mode.
  const canDecide = useCan('billing.manage')

  const requests = useQuery({
    queryKey: [...OVERRIDE_REQUESTS, billId],
    queryFn: () => fetchOverrideRequests(billId),
    enabled: bill.bill_override_requests_enabled,
  })
  const inlineStatus = useQuery({
    queryKey: [...INLINE_ADMIN, billId],
    queryFn: () => fetchInlineAdminStatus(billId),
    enabled: bill.inline_admin_bill_edit_enabled,
    // Short — the window's own countdown has to stay honest, the same reasoning the Admin
    // Mode switcher's own poll already uses.
    refetchInterval: 15_000,
  })

  // "Ends on... leaving the bill": best-effort, fire-and-forget — a window this screen opened
  // stops here rather than sitting live until its own idle timeout.
  useEffect(() => {
    return () => {
      if (inlineStatus.data?.active) void releaseInlineAdmin(billId)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [billId])

  const invalidateBill = (updated?: Bill) => {
    if (updated) queryClient.setQueryData([...BILL, billId], updated)
    else queryClient.invalidateQueries({ queryKey: [...BILL, billId] })
    queryClient.invalidateQueries({ queryKey: [...OVERRIDE_REQUESTS, billId] })
  }

  return (
    <div className="space-y-4">
      {bill.bill_override_requests_enabled && (
        <OverrideRequestsCard
          billId={billId}
          requests={requests.data ?? []}
          canDecide={canDecide}
          onChanged={invalidateBill}
        />
      )}
      {bill.inline_admin_bill_edit_enabled && (
        <InlineAdminCard
          billId={billId}
          status={inlineStatus.data ?? { active: false, admin_email: null, hard_limit_at: null }}
          onAuthenticated={() => queryClient.invalidateQueries({ queryKey: [...INLINE_ADMIN, billId] })}
          onSaved={(updated) => {
            invalidateBill(updated)
            queryClient.invalidateQueries({ queryKey: [...INLINE_ADMIN, billId] })
          }}
        />
      )}
    </div>
  )
}

const STATUS_VARIANT: Record<OverrideRequest['status'], 'secondary' | 'default' | 'destructive'> = {
  pending: 'secondary',
  approved: 'default',
  rejected: 'destructive',
}

function OverrideRequestsCard({
  billId,
  requests,
  canDecide,
  onChanged,
}: {
  billId: string
  requests: OverrideRequest[]
  canDecide: boolean
  onChanged: (updated?: Bill) => void
}) {
  const [open, setOpen] = useState(false)
  const [kind, setKind] = useState<'discount' | 'price_override'>('discount')
  const [amount, setAmount] = useState('')
  const [reason, setReason] = useState('')

  const submit = useMutation({
    mutationFn: () => {
      const cents = dollarsToCents(amount)
      if (cents === null) throw new Error('Enter a valid amount')
      return requestBillOverride(billId, { kind, requested_total_cents: cents, reason })
    },
    onSuccess: () => {
      onChanged()
      setOpen(false)
      setAmount('')
      setReason('')
    },
  })

  const pending = requests.filter((r) => r.status === 'pending')

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle>Exception requests</CardTitle>
        <Dialog open={open} onOpenChange={setOpen}>
          <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
            Request an exception
          </Button>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Request an exception</DialogTitle>
              <DialogDescription>
                Ask an admin/owner to authorize a price outside what the predefined discounts
                cover. They can approve it as asked, revise the amount, or reject it.
              </DialogDescription>
            </DialogHeader>
            <Form onSubmit={() => submit.mutate()}>
              <Field label="Kind" htmlFor="override-kind">
                <Select value={kind} onValueChange={(v) => setKind(v as typeof kind)}>
                  <SelectTrigger id="override-kind">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="discount">Ad hoc discount</SelectItem>
                    <SelectItem value="price_override">Price override</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
              <Field label="Proposed total" htmlFor="override-amount">
                <Input
                  id="override-amount"
                  inputMode="decimal"
                  placeholder="0.00"
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                />
              </Field>
              <Field label="Reason" htmlFor="override-reason">
                <Textarea
                  id="override-reason"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  required
                />
              </Field>
              {submit.isError && (
                <FormError>
                  {submit.error instanceof Error ? submit.error.message : 'Could not submit this request'}
                </FormError>
              )}
              <DialogFooter>
                <Button type="submit" disabled={submit.isPending}>
                  Submit
                </Button>
              </DialogFooter>
            </Form>
          </DialogContent>
        </Dialog>
      </CardHeader>
      <CardContent className="space-y-3">
        {requests.length === 0 ? (
          <p className="text-sm text-muted-foreground">No exceptions requested for this bill.</p>
        ) : (
          <ul className="space-y-3">
            {requests.map((request) => (
              <OverrideRequestRow key={request.id} request={request} canDecide={canDecide} onChanged={onChanged} />
            ))}
          </ul>
        )}
        {pending.length > 0 && !canDecide && (
          <p className="text-xs text-muted-foreground">
            Waiting for an admin/owner to review {pending.length === 1 ? 'this request' : 'these requests'}.
          </p>
        )}
      </CardContent>
    </Card>
  )
}

function OverrideRequestRow({
  request,
  canDecide,
  onChanged,
}: {
  request: OverrideRequest
  canDecide: boolean
  onChanged: (updated?: Bill) => void
}) {
  const [revised, setRevised] = useState('')
  const [note, setNote] = useState('')

  const decide = useMutation({
    mutationFn: (decision: 'approved' | 'rejected') => {
      const body: Parameters<typeof decideOverrideRequest>[2] = { decision }
      if (decision === 'approved' && revised.trim() !== '') {
        const cents = dollarsToCents(revised)
        if (cents === null) throw new Error('Enter a valid revised amount')
        body.decided_total_cents = cents
      }
      if (note.trim() !== '') body.note = note
      return decideOverrideRequest(request.bill_id, request.id, body)
    },
    onSuccess: () => onChanged(),
  })

  return (
    <li className="space-y-2 rounded-lg border p-3 text-sm">
      <div className="flex items-center justify-between">
        <span>
          {request.kind === 'discount' ? 'Ad hoc discount' : 'Price override'} to{' '}
          <span className="font-medium tabular-nums">{money(request.requested_total_cents)}</span>
        </span>
        <Badge variant={STATUS_VARIANT[request.status]}>{request.status}</Badge>
      </div>
      <p className="text-muted-foreground">
        {request.requested_by_email} — {request.reason}
      </p>
      {request.status !== 'pending' && (
        <p className="text-xs text-muted-foreground">
          {request.status === 'approved' ? 'Approved' : 'Rejected'} by {request.decided_by_email}
          {request.decided_total_cents !== null && ` at ${money(request.decided_total_cents)}`}
          {request.decision_note && ` — ${request.decision_note}`}
        </p>
      )}
      {request.status === 'pending' && canDecide && (
        <div className="flex flex-wrap items-end gap-2 pt-1">
          <div className="w-32">
            <Label htmlFor={`revise-${request.id}`} className="text-xs">
              Revise to (optional)
            </Label>
            <Input
              id={`revise-${request.id}`}
              inputMode="decimal"
              placeholder="0.00"
              value={revised}
              onChange={(e) => setRevised(e.target.value)}
            />
          </div>
          <div className="flex-1">
            <Label htmlFor={`note-${request.id}`} className="text-xs">
              Note (optional)
            </Label>
            <Input id={`note-${request.id}`} value={note} onChange={(e) => setNote(e.target.value)} />
          </div>
          <Button size="sm" disabled={decide.isPending} onClick={() => decide.mutate('approved')}>
            Approve
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={decide.isPending}
            onClick={() => decide.mutate('rejected')}
          >
            Reject
          </Button>
        </div>
      )}
      {decide.isError && (
        <FormError>
          {decide.error instanceof Error ? decide.error.message : 'Could not record this decision'}
        </FormError>
      )}
    </li>
  )
}

function InlineAdminCard({
  billId,
  status,
  onAuthenticated,
  onSaved,
}: {
  billId: string
  status: { active: boolean; admin_email: string | null; hard_limit_at: string | null }
  onAuthenticated: () => void
  onSaved: (updated: Bill) => void
}) {
  const [open, setOpen] = useState(false)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [totp, setTotp] = useState('')
  const [mfaNeeded, setMfaNeeded] = useState(false)
  const [amount, setAmount] = useState('')
  const [reason, setReason] = useState('')

  const authenticate = useMutation({
    mutationFn: () =>
      authenticateInlineAdmin(billId, { email, password, totp: totp || undefined }),
    onSuccess: () => {
      setOpen(false)
      setPassword('')
      setTotp('')
      setMfaNeeded(false)
      onAuthenticated()
    },
    onError: (error) => {
      if (error instanceof ApiError && error.code === 'mfa_required') setMfaNeeded(true)
    },
  })

  const release = useMutation({
    mutationFn: () => releaseInlineAdmin(billId),
    onSuccess: onAuthenticated,
  })

  const save = useMutation({
    mutationFn: () => {
      const cents = dollarsToCents(amount)
      if (cents === null) throw new Error('Enter a valid amount')
      return applyInlineAdminEdit(billId, { total_cents: cents, reason })
    },
    onSuccess: (updated) => {
      setAmount('')
      setReason('')
      onSaved(updated)
    },
  })

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle>Inline admin edit</CardTitle>
        {!status.active && (
          <Dialog open={open} onOpenChange={setOpen}>
            <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
              Sign in as admin
            </Button>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Sign in as admin/owner</DialogTitle>
                <DialogDescription>
                  An admin or owner enters their own credentials to make a supervised edit here,
                  under their own identity. This does not change who is signed in on this screen.
                </DialogDescription>
              </DialogHeader>
              <Form onSubmit={() => authenticate.mutate()}>
                <Field label="Email" htmlFor="inline-admin-email">
                  <Input
                    id="inline-admin-email"
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    required
                  />
                </Field>
                <Field label="Password" htmlFor="inline-admin-password">
                  <Input
                    id="inline-admin-password"
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    required
                  />
                </Field>
                {mfaNeeded && (
                  <Field label="Authenticator code" htmlFor="inline-admin-totp">
                    <Input
                      id="inline-admin-totp"
                      value={totp}
                      onChange={(e) => setTotp(e.target.value)}
                      autoFocus
                    />
                  </Field>
                )}
                {authenticate.isError && (
                  <FormError>
                    {authenticate.error instanceof Error
                      ? authenticate.error.message
                      : 'Could not authenticate'}
                  </FormError>
                )}
                <DialogFooter>
                  <Button type="submit" disabled={authenticate.isPending}>
                    Continue
                  </Button>
                </DialogFooter>
              </Form>
            </DialogContent>
          </Dialog>
        )}
      </CardHeader>
      <CardContent className="space-y-3">
        {!status.active ? (
          <p className="text-sm text-muted-foreground">
            An admin/owner can sign in here to make a supervised correction, without changing who
            is signed in on this screen.
          </p>
        ) : (
          <>
            <div className="flex items-center justify-between rounded-lg border bg-muted/40 p-3 text-sm">
              <span className="inline-flex items-center gap-1.5">
                <ShieldCheck className="size-4 text-primary" aria-hidden />
                Editing as <span className="font-medium">{status.admin_email}</span>
                {status.hard_limit_at && (
                  <span className="text-muted-foreground">
                    · until {new Date(status.hard_limit_at).toLocaleTimeString()}
                  </span>
                )}
              </span>
              <Button variant="ghost" size="sm" disabled={release.isPending} onClick={() => release.mutate()}>
                Cancel
              </Button>
            </div>
            <Form onSubmit={() => save.mutate()}>
              <Field label="New total" htmlFor="inline-admin-amount">
                <Input
                  id="inline-admin-amount"
                  inputMode="decimal"
                  placeholder="0.00"
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                  required
                />
              </Field>
              <Field label="Reason" htmlFor="inline-admin-reason">
                <Textarea
                  id="inline-admin-reason"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  required
                />
              </Field>
              {save.isError && (
                <FormError>
                  {save.error instanceof Error ? save.error.message : 'Could not save this edit'}
                </FormError>
              )}
              <DialogFooter>
                <Button type="submit" disabled={save.isPending}>
                  Save
                </Button>
              </DialogFooter>
            </Form>
          </>
        )}
      </CardContent>
    </Card>
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
