import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { PackageOpen } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { EmptyState } from '@/components/empty-state'
import { Field, Form, FormError } from '@/components/form'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
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
  fetchClientPackagePurchases,
  fetchSellablePackages,
  purchasePackage,
  type ClientPackagePurchase,
  type SellablePackageRow,
} from '@/lib/api'
import { centsToDollars } from '@/lib/money'
import { CLIENT_PACKAGE_PURCHASES, SELLABLE_PACKAGES } from '@/lib/query-keys'

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
 * per service, expiry and status, plus *Sell package* (story 52-53). Behind `billing.view` —
 * `clients.tsx` is what decides whether this card renders, the same gate `ClientInvoicesCard`
 * uses. Refund (#109) and transfer (#111, spec #96) are later tickets' own per-purchase
 * actions; `ClientPackagePurchase.customer_id` is already shaped so #111 can add a derived
 * holder and transfer chain onto this same row without a rename.
 */
export function ClientPackagesCard({ customerId }: { customerId: string }) {
  const [sellOpen, setSellOpen] = useState(false)
  const query = useQuery({
    queryKey: [...CLIENT_PACKAGE_PURCHASES, customerId],
    queryFn: () => fetchClientPackagePurchases(customerId),
  })

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
                  <TableHead className="pr-4">Status</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {query.data.map((row) => (
                  <PackagePurchaseRow key={row.id} row={row} />
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

function PackagePurchaseRow({ row }: { row: ClientPackagePurchase }) {
  const status = deriveStatus(row)
  const { label, variant } = STATUS[status]
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
      <TableCell className="pr-4">
        <Badge variant={variant}>{label}</Badge>
      </TableCell>
    </TableRow>
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
