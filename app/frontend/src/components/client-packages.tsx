import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { PackageOpen } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { toast } from 'sonner'
import { ChoiceSelect } from '@/components/choice-select'
import { EmptyState } from '@/components/empty-state'
import { Field, Form, FormError } from '@/components/form'
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import {
  fetchClientPackagePurchases,
  fetchInvoice,
  fetchSellablePackages,
  purchasePackage,
  refundPackagePurchase,
  type ClientPackagePurchase,
  type SellablePackageRow,
} from '@/lib/api'
import { useCan } from '@/lib/capability-gate'
import { centsToDollars, dollarsToCents } from '@/lib/money'
import { CLIENT_PACKAGE_PURCHASES, INVOICE, SELLABLE_PACKAGES } from '@/lib/query-keys'

const money = (cents: number) => `$${centsToDollars(cents)}`

type PurchaseStatus = 'active' | 'unpaid' | 'expired' | 'used_up' | 'refunded'

/** Derived client-side, the same way the Invoices list's own status is (`routes/invoice-view
 *  .tsx::deriveListStatus`) — the server sends the raw facts (`credits_activated`,
 *  `credits_voided_at`, `expires_at`, remaining per service), never a stored label. */
function deriveStatus(row: ClientPackagePurchase): PurchaseStatus {
  if (row.credits_voided_at) return 'refunded'
  if (!row.credits_activated) return 'unpaid'
  if (row.expires_at && row.expires_at < new Date().toISOString().slice(0, 10)) return 'expired'
  if (row.credits.every((c) => c.credits_remaining === 0)) return 'used_up'
  return 'active'
}

const STATUS: Record<PurchaseStatus, { label: string; variant: 'success' | 'warning' | 'outline' }> = {
  active: { label: 'Active', variant: 'success' },
  unpaid: { label: 'Unpaid', variant: 'warning' },
  expired: { label: 'Expired', variant: 'outline' },
  used_up: { label: 'Fully used', variant: 'outline' },
  refunded: { label: 'Refunded', variant: 'outline' },
}

/**
 * A client's Packages tab (spec #95 user story 54; #108): purchases with remaining credits
 * per service, expiry and status, plus *Sell package* (story 52-53) and *Refund* (#109, story
 * 55-56). Behind `billing.view` — `clients.tsx` is what decides whether this card renders, the
 * same gate `ClientInvoicesCard` uses. `purchaserName` is who paid, for the refund dialog's own
 * wording (spec #96: refunds always go to the purchaser) — for now the purchaser is always the
 * customer this card is rendered for, since no transfer exists yet; #111 will pass the holder
 * separately once one can. Transfer (#111, spec #96) is a later ticket's own per-purchase
 * action; `ClientPackagePurchase.customer_id` is already shaped so #111 can add a derived
 * holder and transfer chain onto this same row without a rename.
 */
export function ClientPackagesCard({
  customerId,
  purchaserName,
}: {
  customerId: string
  purchaserName: string
}) {
  const [sellOpen, setSellOpen] = useState(false)
  const canRefund = useCan('billing.manage')
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: [...CLIENT_PACKAGE_PURCHASES, customerId],
    queryFn: () => fetchClientPackagePurchases(customerId),
  })

  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: [...CLIENT_PACKAGE_PURCHASES, customerId] })

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle className="text-sm font-medium">Packages</CardTitle>
        <Button size="sm" onClick={() => setSellOpen(true)}>
          Sell package
        </Button>
      </CardHeader>
      <CardContent>
        {query.isPending ? (
          <Skeleton className="h-32 w-full" />
        ) : query.isError ? (
          <p role="alert" className="text-destructive">
            {query.error.message}
          </p>
        ) : query.data.length === 0 ? (
          <EmptyState
            icon={PackageOpen}
            title="No packages yet"
            description="Packages sold to this client will list here."
          />
        ) : (
          <div className="rounded-xl border">
            <Table aria-label="Packages">
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-4">Package</TableHead>
                  <TableHead>Credits remaining</TableHead>
                  <TableHead>Expiry</TableHead>
                  <TableHead className={canRefund ? '' : 'pr-4'}>Status</TableHead>
                  {canRefund && <TableHead className="pr-4">Actions</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {query.data.map((row) => (
                  <PackagePurchaseRow
                    key={row.id}
                    row={row}
                    canRefund={canRefund}
                    purchaserName={purchaserName}
                    onRefunded={refresh}
                  />
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>

      <SellPackageDialog customerId={customerId} open={sellOpen} onOpenChange={setSellOpen} />
    </Card>
  )
}

function PackagePurchaseRow({
  row,
  canRefund,
  purchaserName,
  onRefunded,
}: {
  row: ClientPackagePurchase
  canRefund: boolean
  purchaserName: string
  onRefunded: () => void
}) {
  const status = deriveStatus(row)
  const { label, variant } = STATUS[status]
  const [refunding, setRefunding] = useState(false)
  return (
    <TableRow className="h-12">
      <TableCell className="pl-4">
        <Link to={`/bills/invoices/${row.invoice_id}`} className="font-medium hover:underline">
          {row.name}
        </Link>
      </TableCell>
      <TableCell>
        <ul className="text-sm">
          {row.credits.map((c) => (
            <li key={c.service_id}>
              {c.service_name}: {c.credits_remaining} of {c.credits_total} left
            </li>
          ))}
        </ul>
      </TableCell>
      <TableCell className="text-sm text-muted-foreground">
        {row.expires_at ? (
          <time dateTime={row.expires_at}>{formatDate(row.expires_at)}</time>
        ) : (
          'Never'
        )}
      </TableCell>
      <TableCell className={canRefund ? '' : 'pr-4'}>
        <Badge variant={variant}>{label}</Badge>
      </TableCell>
      {canRefund && (
        <TableCell className="pr-4">
          <Button size="sm" variant="outline" onClick={() => setRefunding(true)}>
            Refund
          </Button>
        </TableCell>
      )}
      {refunding && (
        <RefundPurchaseDialog
          purchase={row}
          purchaserName={purchaserName}
          onClose={() => setRefunding(false)}
          onRefunded={onRefunded}
        />
      )}
    </TableRow>
  )
}

type CreditsChoice = 'default' | 'keep' | 'cancel'

/**
 * *Refund* (#109, spec #95 stories 55-56; spec #96: refunds go to the purchaser, who paid).
 * Pre-selects the server's standard values: no credit redeemed on the purchase (already known
 * from `row.credits`, no extra read) pre-selects the standard, whole-invoice refund, read via
 * the invoice's own balance (`GET /api/invoices/{id}`, already the invoice view's own read —
 * `money received less prior refunds` is `grand_total_cents - outstanding_cents` for a package
 * invoice, which never carries a prepaid line). Any credit redeemed forces the manual
 * exception, matching `billing/package_refund.py`'s own refusal. The manual exception offers
 * exactly the choices the route supports: an explicit amount, the remaining-credit choice
 * (server default: cancel on a full refund, keep on a partial one) and the commission choice
 * (server default: preserve).
 */
function RefundPurchaseDialog({
  purchase,
  purchaserName,
  onClose,
  onRefunded,
}: {
  purchase: ClientPackagePurchase
  purchaserName: string
  onClose: () => void
  onRefunded: () => void
}) {
  const redeemed = purchase.credits.some((c) => c.credits_used > 0)
  const invoiceQuery = useQuery({
    queryKey: [...INVOICE, purchase.invoice_id],
    queryFn: () => fetchInvoice(purchase.invoice_id),
  })
  const standardCents = invoiceQuery.data
    ? Math.max(invoiceQuery.data.grand_total_cents - invoiceQuery.data.outstanding_cents, 0)
    : null

  const [exception, setException] = useState(redeemed)
  const [reason, setReason] = useState('')
  const [amount, setAmount] = useState('')
  const [creditsChoice, setCreditsChoice] = useState<CreditsChoice>('default')
  const [reverseCommission, setReverseCommission] = useState(false)

  // Pre-selected the moment the standard amount is known — a fresh purchase never overwrites
  // an amount the admin already edited while switching modes, since this only fires while the
  // standard (non-exception) amount is the one shown.
  useEffect(() => {
    if (!exception && standardCents !== null) setAmount(centsToDollars(standardCents))
  }, [exception, standardCents])

  const amountCents = dollarsToCents(amount)
  const incomplete = !reason.trim() || (exception && (amountCents === null || amountCents <= 0))

  const save = useMutation({
    mutationFn: () =>
      refundPackagePurchase(purchase.id, {
        reason: reason.trim(),
        exception: exception
          ? {
              amount_cents: amountCents as number,
              cancel_remaining_credits: creditsChoice === 'default' ? null : creditsChoice === 'cancel',
              reverse_commission: reverseCommission,
            }
          : undefined,
      }),
    onSuccess: () => {
      toast.success('Package refunded')
      onRefunded()
      onClose()
    },
  })

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Refund {purchase.name}</DialogTitle>
          <DialogDescription>Refunds {purchaserName}, who paid.</DialogDescription>
        </DialogHeader>

        <Form onSubmit={() => !incomplete && save.mutate()}>
          {redeemed ? (
            <p className="text-sm text-muted-foreground">
              A credit has been used, so this package is no longer refundable under the standard
              policy — only a manual exception can refund it.
            </p>
          ) : (
            <div className="flex items-center gap-2">
              <Checkbox
                id="refund-exception"
                checked={exception}
                onCheckedChange={(on) => setException(on === true)}
              />
              <Label htmlFor="refund-exception" className="font-normal">
                Manual exception
              </Label>
            </div>
          )}

          {exception ? (
            <>
              <Field label="Amount" htmlFor="refund-amount">
                <Input
                  id="refund-amount"
                  inputMode="decimal"
                  className="tabular-nums"
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                />
              </Field>
              <Field label="Remaining credits" htmlFor="refund-credits">
                <ChoiceSelect
                  id="refund-credits"
                  value={creditsChoice}
                  onValueChange={(v) => setCreditsChoice(v as CreditsChoice)}
                  options={[
                    {
                      value: 'default',
                      label: 'Server default (cancel on a full refund, keep on a partial one)',
                    },
                    { value: 'keep', label: 'Keep remaining credits' },
                    { value: 'cancel', label: 'Cancel remaining credits' },
                  ]}
                />
              </Field>
              <div className="flex items-center gap-2">
                <Checkbox
                  id="refund-reverse-commission"
                  checked={reverseCommission}
                  onCheckedChange={(on) => setReverseCommission(on === true)}
                />
                <Label htmlFor="refund-reverse-commission" className="font-normal">
                  Reverse commission already earned
                </Label>
              </div>
            </>
          ) : (
            <p className="text-sm">
              Standard refund:{' '}
              <span className="tabular-nums">
                {standardCents === null ? '…' : money(standardCents)}
              </span>
            </p>
          )}

          <Field label="Reason" htmlFor="refund-reason">
            <Textarea id="refund-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>

          {save.error && <FormError>{save.error.message}</FormError>}

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={incomplete || save.isPending}>
              {save.isPending ? 'Refunding…' : 'Refund'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}

function formatDate(isoDate: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date(isoDate))
}

/**
 * *Sell package* (story 52-53): pick an active package, confirm the price and credits,
 * purchase, then land on the package invoice with the payment dialog open — `?pay=1`, the
 * same query `invoice-payments-panel.tsx` reads to auto-open on arrival for Sell (#106) and
 * here alike.
 */
function SellPackageDialog({
  customerId,
  open,
  onOpenChange,
}: {
  customerId: string
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [selectedId, setSelectedId] = useState('')

  const packages = useQuery({
    queryKey: SELLABLE_PACKAGES,
    queryFn: fetchSellablePackages,
    enabled: open,
  })

  useEffect(() => {
    if (open) setSelectedId('')
  }, [open])

  const selected: SellablePackageRow | null =
    packages.data?.find((p) => p.id === selectedId) ?? null

  const sell = useMutation({
    mutationFn: () => purchasePackage(selectedId, customerId),
    onSuccess: (purchase) => {
      queryClient.invalidateQueries({ queryKey: [...CLIENT_PACKAGE_PURCHASES, customerId] })
      onOpenChange(false)
      navigate(`/bills/invoices/${purchase.invoice_id}?pay=1`)
    },
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Sell a package</DialogTitle>
          <DialogDescription>Pick an active package to sell this client.</DialogDescription>
        </DialogHeader>

        <Form onSubmit={() => selectedId && sell.mutate()}>
          <Field label="Package" htmlFor="sell-package">
            {packages.isPending ? (
              <Skeleton className="h-9 w-full" />
            ) : packages.data && packages.data.length === 0 ? (
              <p className="text-sm text-muted-foreground">No active packages to sell.</p>
            ) : (
              <Select value={selectedId} onValueChange={setSelectedId}>
                <SelectTrigger id="sell-package">
                  <SelectValue placeholder="Choose a package" />
                </SelectTrigger>
                <SelectContent>
                  {packages.data?.map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.name} — {money(p.price_cents)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </Field>

          {selected && (
            <div className="flex flex-col gap-1 rounded-lg border bg-muted/40 p-3 text-sm">
              <div className="flex justify-between font-medium">
                <span>Price</span>
                <span className="tabular-nums">
                  {money(selected.price_cents)}
                  {selected.tax_convention === 'inclusive' ? ' (tax included)' : ' + tax'}
                </span>
              </div>
              <ul className="text-muted-foreground">
                {selected.services.map((s) => (
                  <li key={s.service_id}>
                    {s.credits} × {s.service_name}
                  </li>
                ))}
              </ul>
              <p className="text-muted-foreground">
                {selected.expires_after_days
                  ? `Expires ${selected.expires_after_days} days after purchase`
                  : 'Never expires'}
              </p>
            </div>
          )}

          {sell.error && <FormError>{sell.error.message}</FormError>}

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!selectedId || sell.isPending}>
              {sell.isPending ? 'Selling…' : 'Sell package'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
