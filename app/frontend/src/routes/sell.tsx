import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Search, ShoppingCart, Trash2 } from 'lucide-react'
import { useDeferredValue, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import { EmptyState } from '@/components/empty-state'
import { FormError } from '@/components/form'
import { ReplacesInvoiceBanner } from '@/components/replaces-invoice-banner'
import { ClientFilter } from '@/routes/invoices'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import {
  ApiError,
  addRetailSaleLine,
  applyRetailSaleDiscounts,
  fetchOpenRetailSales,
  fetchProductCatalog,
  fetchRetailSale,
  issueRetailSale,
  removeRetailSaleLine,
  startRetailSale,
  stockConflict,
  type CatalogProduct,
  type Customer,
  type DiscountChoice,
  type RetailSale,
} from '@/lib/api'
import { money } from '@/lib/money'
import { OPEN_RETAIL_SALES, PRODUCT_CATALOG, RETAIL_SALE } from '@/lib/query-keys'


type CustomerHint = { id: string; name: string }

/**
 * Retail checkout (#106, spec #95 user stories 42-50): start a sale with or without a client,
 * resume an open draft, search products and add them with a quantity, apply an eligible
 * discount, and issue — landing on the retail invoice with the payment dialog open.
 *
 * **`?sale={id}` is the draft's own address** — set once a sale is started or resumed, so a
 * reload of this exact URL re-reads the same draft from the server (never local state, the
 * server is the only source of truth for a cart). It is also the URL #107's retail cancel &
 * replace will open its replacement draft on: `/sell?sale={id}`.
 */
export function SellPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const saleId = searchParams.get('sale')
  const [hint, setHint] = useState<CustomerHint | null>(null)

  const open = (id: string, customer: CustomerHint | null) => {
    setHint(customer)
    setSearchParams({ sale: id }, { replace: true })
  }
  const close = () => {
    setHint(null)
    setSearchParams({}, { replace: true })
  }

  return saleId ? (
    <SaleCart saleId={saleId} customerHint={hint} onClosed={close} />
  ) : (
    <StartSale onStarted={open} />
  )
}

function StartSale({ onStarted }: { onStarted: (id: string, customer: CustomerHint | null) => void }) {
  const [customer, setCustomer] = useState<Customer | null>(null)
  const drafts = useQuery({ queryKey: OPEN_RETAIL_SALES, queryFn: fetchOpenRetailSales })

  const start = useMutation({
    mutationFn: () => startRetailSale(customer?.id ?? null),
    onSuccess: (sale) =>
      onStarted(sale.id, customer ? { id: customer.id, name: `${customer.first_name} ${customer.last_name}` } : null),
  })

  return (
    <div className="mx-auto max-w-xl space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShoppingCart className="size-4 text-muted-foreground" aria-hidden />
            Start a sale
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <p className="text-sm font-medium">Client (optional)</p>
            <ClientFilter customer={customer} onChange={setCustomer} />
          </div>
          {start.isError && (
            <FormError>
              {start.error instanceof Error ? start.error.message : 'Could not start this sale'}
            </FormError>
          )}
          <Button onClick={() => start.mutate()} disabled={start.isPending}>
            {customer ? `Start sale for ${customer.first_name} ${customer.last_name}` : 'Start walk-in sale'}
          </Button>
        </CardContent>
      </Card>

      <div>
        <h2 className="mb-2 text-sm font-medium text-muted-foreground">Open drafts</h2>
        {drafts.isPending ? (
          <Skeleton className="h-20 w-full" />
        ) : drafts.isError ? (
          <p role="alert" className="text-destructive">
            {drafts.error.message}
          </p>
        ) : drafts.data.length === 0 ? (
          <p className="text-sm text-muted-foreground">No sales in progress.</p>
        ) : (
          <ul className="divide-y rounded-xl border bg-card">
            {drafts.data.map((draft) => (
              <li key={draft.id}>
                <button
                  type="button"
                  className="flex w-full items-center justify-between px-4 py-2.5 text-left text-sm hover:bg-accent"
                  onClick={() =>
                    onStarted(
                      draft.id,
                      draft.customer_id && draft.customer_name
                        ? { id: draft.customer_id, name: draft.customer_name }
                        : null,
                    )
                  }
                >
                  <span className="font-medium">{draft.customer_name ?? 'Walk-in'}</span>
                  <span className="tabular-nums text-muted-foreground">
                    {draft.line_count === 1 ? '1 item' : `${draft.line_count} items`}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

type VariantMatch = { product: CatalogProduct; variant: CatalogProduct['variants'][number] }

function SaleCart({
  saleId,
  customerHint,
  onClosed,
}: {
  saleId: string
  customerHint: CustomerHint | null
  onClosed: () => void
}) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [quantities, setQuantities] = useState<Record<string, string>>({})
  const term = useDeferredValue(search.trim().toLowerCase())

  const sale = useQuery({
    queryKey: [...RETAIL_SALE, saleId],
    queryFn: () => fetchRetailSale(saleId),
  })
  const catalog = useQuery({ queryKey: PRODUCT_CATALOG, queryFn: fetchProductCatalog })

  const variantsById = new Map<string, VariantMatch>()
  for (const product of catalog.data ?? []) {
    for (const variant of product.variants) variantsById.set(variant.id, { product, variant })
  }

  const onSaleUpdated = (updated: RetailSale) => {
    queryClient.setQueryData([...RETAIL_SALE, saleId], updated)
    queryClient.invalidateQueries({ queryKey: OPEN_RETAIL_SALES })
  }

  const addLine = useMutation({
    mutationFn: ({ variantId, quantity }: { variantId: string; quantity: number }) =>
      addRetailSaleLine(saleId, variantId, quantity),
    onSuccess: onSaleUpdated,
  })
  const removeLine = useMutation({
    mutationFn: (lineId: string) => removeRetailSaleLine(saleId, lineId),
    onSuccess: onSaleUpdated,
  })
  const applyDiscounts = useMutation({
    mutationFn: (discountIds: string[]) => applyRetailSaleDiscounts(saleId, discountIds),
    onSuccess: onSaleUpdated,
  })
  const issue = useMutation({
    mutationFn: () => issueRetailSale(saleId),
    onSuccess: (invoice) => {
      queryClient.invalidateQueries({ queryKey: OPEN_RETAIL_SALES })
      // #103: the payments panel opens `RecordPaymentDialog` pre-filled when `pay=1` is
      // present, so selling and taking payment is one flow (spec #95 story 49).
      navigate(`/bills/invoices/${invoice.id}?kind=retail&pay=1`)
    },
  })

  if (sale.isPending) return <Skeleton className="h-64 w-full" />
  if (sale.isError) {
    const message =
      sale.error instanceof ApiError && sale.error.status === 404 ? 'No such sale.' : sale.error.message
    return (
      <p role="alert" className="text-destructive">
        {message}
      </p>
    )
  }

  const data = sale.data
  const conflict = stockConflict(issue.error)
  const clientLabel = customerHint?.name ?? (data.customer_id ? 'Client attached' : 'Walk-in')

  const matches: VariantMatch[] =
    term.length === 0
      ? []
      : (catalog.data ?? [])
          .flatMap((product) => product.variants.map((variant) => ({ product, variant })))
          .filter(
            ({ product, variant }) =>
              product.name.toLowerCase().includes(term) ||
              variant.name.toLowerCase().includes(term) ||
              variant.sku.toLowerCase().includes(term) ||
              (variant.barcode ?? '').toLowerCase().includes(term),
          )
          .slice(0, 8)

  const addVariant = (variantId: string) => {
    const raw = Number.parseInt(quantities[variantId] ?? '1', 10)
    const quantity = Number.isFinite(raw) && raw > 0 ? raw : 1
    addLine.mutate({ variantId, quantity })
    setSearch('')
  }

  const toggleDiscount = (discountId: string, checked: boolean) => {
    const selected = new Set(data.eligible_discounts.filter((d) => d.applied).map((d) => d.id))
    if (checked) selected.add(discountId)
    else selected.delete(discountId)
    applyDiscounts.mutate([...selected])
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <button
        type="button"
        onClick={onClosed}
        className="inline-flex items-center gap-1.5 text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-4" aria-hidden />
        Start a new sale
      </button>

      <div>
        <h1 className="text-lg font-semibold">{clientLabel}</h1>
        <p className="text-sm text-muted-foreground">Draft sale</p>
      </div>

      <ReplacesInvoiceBanner replacesInvoiceId={data.replaces_retail_invoice_id} kind="retail" />

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Search className="size-4 text-muted-foreground" aria-hidden />
            Add a product
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <Label htmlFor="sell-search" className="sr-only">
            Search products
          </Label>
          <Input
            id="sell-search"
            placeholder="Name, SKU or barcode"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          {matches.length > 0 && (
            <ul className="divide-y rounded-lg border">
              {matches.map(({ product, variant }) => (
                <li key={variant.id} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
                  <div>
                    <p className="font-medium">
                      {product.name} — {variant.name}
                    </p>
                    <p className="tabular-nums text-muted-foreground">
                      {money(variant.price_cents)} · {variant.quantity_on_hand} on hand
                      {variant.is_low_stock && (
                        <Badge variant="warning" className="ml-2">
                          Low
                        </Badge>
                      )}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Label htmlFor={`qty-${variant.id}`} className="sr-only">
                      Quantity
                    </Label>
                    <Input
                      id={`qty-${variant.id}`}
                      type="number"
                      min={1}
                      className="w-16"
                      value={quantities[variant.id] ?? '1'}
                      onChange={(e) =>
                        setQuantities((q) => ({ ...q, [variant.id]: e.target.value }))
                      }
                    />
                    <Button size="sm" disabled={addLine.isPending} onClick={() => addVariant(variant.id)}>
                      Add
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
          {addLine.isError && (
            <FormError>
              {addLine.error instanceof Error ? addLine.error.message : 'Could not add this product'}
            </FormError>
          )}
        </CardContent>
      </Card>

      {data.lines.length === 0 ? (
        <EmptyState
          icon={ShoppingCart}
          title="No products yet"
          description="Search above to add a product to this sale."
        />
      ) : (
        <div className="rounded-xl border bg-card">
          <Table aria-label="Sale lines">
            <TableHeader>
              <TableRow>
                <TableHead className="pl-4">Product</TableHead>
                <TableHead className="text-right">Qty</TableHead>
                <TableHead className="text-right">Price</TableHead>
                <TableHead className="text-right">Tax</TableHead>
                <TableHead className="text-right">Stock</TableHead>
                <TableHead className="pr-4 text-right">Total</TableHead>
                <TableHead className="w-10" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.lines.map((line) => {
                const info = variantsById.get(line.variant_id)
                return (
                  <TableRow key={line.id} className="h-12">
                    <TableCell className="pl-4">{info?.variant.name ?? 'This product'}</TableCell>
                    <TableCell className="text-right tabular-nums">{line.quantity}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {money(line.unit_price_cents)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-muted-foreground">
                      {money(line.tax.tax_cents)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-muted-foreground">
                      {info?.variant.quantity_on_hand ?? '—'}
                    </TableCell>
                    <TableCell className="pr-4 text-right tabular-nums font-medium">
                      {money(line.line_total_cents)}
                    </TableCell>
                    <TableCell>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        aria-label="Remove line"
                        disabled={removeLine.isPending}
                        onClick={() => removeLine.mutate(line.id)}
                      >
                        <Trash2 className="size-4" aria-hidden />
                      </Button>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </div>
      )}

      {data.eligible_discounts.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Discounts</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <ul className="space-y-2">
              {data.eligible_discounts.map((discount) => (
                <DiscountRow
                  key={discount.id}
                  discount={discount}
                  disabled={applyDiscounts.isPending}
                  onToggle={(checked) => toggleDiscount(discount.id, checked)}
                />
              ))}
            </ul>
            {applyDiscounts.isError && (
              <FormError>
                {applyDiscounts.error instanceof Error
                  ? applyDiscounts.error.message
                  : 'Could not apply these discounts'}
              </FormError>
            )}
          </CardContent>
        </Card>
      )}

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

      <div className="flex flex-col items-end gap-2">
        {conflict && (
          <FormError>
            Only {conflict.available} left of &quot;{conflict.name}&quot;.
          </FormError>
        )}
        {issue.isError && !conflict && (
          <FormError>
            {issue.error instanceof Error ? issue.error.message : 'Could not issue this sale'}
          </FormError>
        )}
        <Button onClick={() => issue.mutate()} disabled={issue.isPending || data.lines.length === 0}>
          Issue &amp; take payment
        </Button>
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
        id={`sell-discount-${discount.id}`}
        checked={discount.applied}
        disabled={disabled}
        onCheckedChange={(checked) => onToggle(checked === true)}
      />
      <Label htmlFor={`sell-discount-${discount.id}`} className="font-normal">
        {discount.name} — {amount}
        {!discount.stackable && (
          <span className="ml-1 text-xs text-muted-foreground">(not stackable with others)</span>
        )}
      </Label>
    </li>
  )
}
