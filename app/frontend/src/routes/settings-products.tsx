import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ClipboardCheck,
  MoreHorizontal,
  Package,
  PackagePlus,
  Pencil,
  Plus,
  Power,
  PowerOff,
} from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { EmptyState } from '@/components/empty-state'
import { Field, Form, FormError } from '@/components/form'
import { TaxSettingsFields } from '@/components/tax-settings'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import {
  ApiError,
  adjustStock,
  createProduct,
  createVariant,
  deactivateProduct,
  deactivateVariant,
  fetchProducts,
  reactivateProduct,
  reactivateVariant,
  receiveStock,
  updateProduct,
  updateVariant,
  type ProductRow,
  type ProductVariantDraft,
  type ProductVariantRow,
} from '@/lib/api'
import { useCan } from '@/lib/capability-gate'
import { centsToDollars, dollarsToCents } from '@/lib/money'
import { PRODUCTS } from '@/lib/query-keys'

/**
 * Settings → Products: the retail catalog (M4 #56, PRD).
 *
 * **A product is a name; a variant is what is actually sold.** "Shampoo" is not a thing
 * anybody buys — "Shampoo, 500ml" is, and only the variant carries a SKU, a barcode, a price
 * and a stock count. Retail checkout (a later ticket) reads only active variants of active
 * products (`GET /api/catalog/products`); this screen also shows inactive ones, because it is
 * the editing surface.
 *
 * **Stock is never edited in place.** Every change is a movement row (#61): a new variant's
 * opening count is sent as a receipt (`inventory.receive`) right after it is created, and
 * editing a variant never touches its count.
 *
 * **Deactivating never deletes.** A product or a variant already sold keeps its row so a
 * historical reference is never orphaned; only new sales stop offering it.
 */
export function ProductsPanel() {
  const [includeInactive, setIncludeInactive] = useState(false)
  const products = useQuery({
    queryKey: [...PRODUCTS, includeInactive],
    queryFn: () => fetchProducts(includeInactive),
    placeholderData: (previous) => previous,
  })
  const [editing, setEditing] = useState<ProductRow | null>(null)
  const [creating, setCreating] = useState(false)
  const [addingVariantTo, setAddingVariantTo] = useState<ProductRow | null>(null)
  const [editingVariant, setEditingVariant] = useState<
    { product: ProductRow; variant: ProductVariantRow } | null
  >(null)
  const [receivingInto, setReceivingInto] = useState<
    { product: ProductRow; variant: ProductVariantRow } | null
  >(null)
  const [adjusting, setAdjusting] = useState<
    { product: ProductRow; variant: ProductVariantRow } | null
  >(null)

  if (products.isPending) return <Skeleton className="h-64 w-full" />
  if (products.isError) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {products.error?.message}
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <p className="max-w-3xl text-sm text-muted-foreground">
          What the business sells at the counter — each product's variants carry their own
          SKU, barcode, price and stock count.
        </p>
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <Checkbox
              id="show-inactive-products"
              checked={includeInactive}
              onCheckedChange={(on) => setIncludeInactive(on === true)}
            />
            <Label htmlFor="show-inactive-products" className="font-normal">
              Show inactive
            </Label>
          </div>
          {products.data?.length !== 0 && (
            <Button onClick={() => setCreating(true)}>
              <Plus aria-hidden />
              Add product
            </Button>
          )}
        </div>
      </div>

      {products.data?.length === 0 ? (
        <EmptyState
          icon={Package}
          title="No products yet"
          description="Add what the business sells at the counter."
          action={
            <Button onClick={() => setCreating(true)}>
              <Plus aria-hidden />
              Add product
            </Button>
          }
        />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Product / variant</TableHead>
              <TableHead>SKU</TableHead>
              <TableHead>Barcode</TableHead>
              <TableHead className="text-right">Price</TableHead>
              <TableHead className="text-right">Stock</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {products.data?.map((product) => (
              <ProductLines
                key={product.id}
                product={product}
                onEditProduct={() => setEditing(product)}
                onAddVariant={() => setAddingVariantTo(product)}
                onEditVariant={(variant) => setEditingVariant({ product, variant })}
                onReceive={(variant) => setReceivingInto({ product, variant })}
                onAdjust={(variant) => setAdjusting({ product, variant })}
              />
            ))}
          </TableBody>
        </Table>
      )}

      {creating && <ProductDialog onClose={() => setCreating(false)} />}
      {editing && <ProductDialog product={editing} onClose={() => setEditing(null)} />}
      {addingVariantTo && (
        <VariantDialog product={addingVariantTo} onClose={() => setAddingVariantTo(null)} />
      )}
      {editingVariant && (
        <VariantDialog
          product={editingVariant.product}
          variant={editingVariant.variant}
          onClose={() => setEditingVariant(null)}
        />
      )}
      {receivingInto && (
        <ReceiveStockDialog
          product={receivingInto.product}
          variant={receivingInto.variant}
          onClose={() => setReceivingInto(null)}
        />
      )}
      {adjusting && (
        <AdjustStockDialog
          product={adjusting.product}
          variant={adjusting.variant}
          onClose={() => setAdjusting(null)}
        />
      )}
    </div>
  )
}

function ProductLines(props: {
  product: ProductRow
  onEditProduct: () => void
  onAddVariant: () => void
  onEditVariant: (variant: ProductVariantRow) => void
  onReceive: (variant: ProductVariantRow) => void
  onAdjust: (variant: ProductVariantRow) => void
}) {
  const { product } = props
  const queryClient = useQueryClient()
  const refresh = () => queryClient.invalidateQueries({ queryKey: PRODUCTS })
  const canReceive = useCan('inventory.receive')
  const canAdjust = useCan('inventory.adjust')

  const setProductActive = useMutation({
    mutationFn: (active: boolean) =>
      active ? reactivateProduct(product.id) : deactivateProduct(product.id),
    onSuccess: (updated) => {
      toast.success(updated.active ? `${updated.name} restored` : `Deactivated ${updated.name}`)
      refresh()
    },
    onError: (error) => {
      toast.error(error.message)
      refresh()
    },
  })

  const setVariantActive = useMutation({
    mutationFn: (vars: { variantId: string; active: boolean }) =>
      vars.active
        ? reactivateVariant(product.id, vars.variantId)
        : deactivateVariant(product.id, vars.variantId),
    onSuccess: (_, vars) => {
      toast.success(vars.active ? 'Variant restored' : 'Variant deactivated')
      refresh()
    },
    onError: (error) => {
      toast.error(error.message)
      refresh()
    },
  })

  return (
    <>
      <TableRow className={product.active ? 'bg-muted/40' : 'bg-muted/40 opacity-55'}>
        <TableCell colSpan={5}>
          <span className="font-medium">{product.name}</span>
          {product.description && (
            <span className="block max-w-md truncate text-xs text-muted-foreground">
              {product.description}
            </span>
          )}
        </TableCell>
        <TableCell>
          {product.active ? (
            <span className="text-muted-foreground">Active</span>
          ) : (
            <Badge variant="secondary">Inactive</Badge>
          )}
        </TableCell>
        <TableCell className="text-right">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="sm" aria-label={`Actions for ${product.name}`}>
                <MoreHorizontal aria-hidden />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-40">
              <DropdownMenuItem onSelect={props.onEditProduct}>
                <Pencil aria-hidden />
                Edit
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={props.onAddVariant}>
                <Plus aria-hidden />
                Add variant
              </DropdownMenuItem>
              {product.active ? (
                <DropdownMenuItem
                  variant="destructive"
                  disabled={setProductActive.isPending}
                  onSelect={() => {
                    if (
                      confirm(
                        `Deactivate ${product.name}?\n\nIt drops off checkout until you restore it. Its variants keep whatever status they already have.`,
                      )
                    )
                      setProductActive.mutate(false)
                  }}
                >
                  <PowerOff aria-hidden />
                  Deactivate
                </DropdownMenuItem>
              ) : (
                <DropdownMenuItem
                  disabled={setProductActive.isPending}
                  onSelect={() => setProductActive.mutate(true)}
                >
                  <Power aria-hidden />
                  Reactivate
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </TableCell>
      </TableRow>
      {product.variants.length === 0 ? (
        <TableRow>
          <TableCell colSpan={7} className="pl-8 text-xs text-muted-foreground">
            No variants yet.
          </TableCell>
        </TableRow>
      ) : (
        product.variants.map((variant) => (
          <TableRow key={variant.id} className={variant.active ? undefined : 'opacity-55'}>
            <TableCell className="pl-8 text-sm">{variant.name}</TableCell>
            <TableCell className="font-mono text-xs">{variant.sku}</TableCell>
            <TableCell className="font-mono text-xs text-muted-foreground">
              {variant.barcode ?? '—'}
            </TableCell>
            <TableCell className="text-right tabular-nums">
              ${centsToDollars(variant.price_cents)}
            </TableCell>
            <TableCell className="text-right tabular-nums">
              {variant.quantity_on_hand}
              {variant.quantity_on_hand <= variant.low_stock_threshold && (
                <Badge variant="warning" className="ml-2">
                  Low
                </Badge>
              )}
            </TableCell>
            <TableCell>
              {variant.active ? (
                <span className="text-muted-foreground">Active</span>
              ) : (
                <Badge variant="secondary">Inactive</Badge>
              )}
            </TableCell>
            <TableCell className="text-right">
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="ghost" size="sm" aria-label={`Actions for ${variant.name}`}>
                    <MoreHorizontal aria-hidden />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="min-w-40">
                  <DropdownMenuItem onSelect={() => props.onEditVariant(variant)}>
                    <Pencil aria-hidden />
                    Edit
                  </DropdownMenuItem>
                  {canReceive && (
                    <DropdownMenuItem onSelect={() => props.onReceive(variant)}>
                      <PackagePlus aria-hidden />
                      Receive stock
                    </DropdownMenuItem>
                  )}
                  {canAdjust && (
                    <DropdownMenuItem onSelect={() => props.onAdjust(variant)}>
                      <ClipboardCheck aria-hidden />
                      Adjust stock
                    </DropdownMenuItem>
                  )}
                  {variant.active ? (
                    <DropdownMenuItem
                      variant="destructive"
                      disabled={setVariantActive.isPending}
                      onSelect={() =>
                        setVariantActive.mutate({ variantId: variant.id, active: false })
                      }
                    >
                      <PowerOff aria-hidden />
                      Deactivate
                    </DropdownMenuItem>
                  ) : (
                    <DropdownMenuItem
                      disabled={setVariantActive.isPending}
                      onSelect={() =>
                        setVariantActive.mutate({ variantId: variant.id, active: true })
                      }
                    >
                      <Power aria-hidden />
                      Reactivate
                    </DropdownMenuItem>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            </TableCell>
          </TableRow>
        ))
      )}
    </>
  )
}

/** Create or edit a product's own fields. Variants are managed from the table's row actions,
 *  never bundled into this save — each has identity of its own (see the module docstring). */
function ProductDialog(props: { product?: ProductRow; onClose: () => void }) {
  const queryClient = useQueryClient()
  const existing = props.product
  const [name, setName] = useState(existing?.name ?? '')
  const [description, setDescription] = useState(existing?.description ?? '')

  const save = useMutation({
    mutationFn: () => {
      const draft = { name: name.trim(), description: description.trim() || null, sort_order: existing?.sort_order ?? 0 }
      return existing ? updateProduct(existing.id, draft) : createProduct(draft)
    },
    onSuccess: (product) => {
      toast.success(existing ? `Saved ${product.name}` : `Added ${product.name}`)
      queryClient.invalidateQueries({ queryKey: PRODUCTS })
      props.onClose()
    },
  })

  const nameConflict = save.error instanceof ApiError && save.error.status === 409
  const incomplete = !name.trim()

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{existing ? `Edit ${existing.name}` : 'Add product'}</DialogTitle>
          <DialogDescription>
            {existing
              ? 'Name and description. Variants are added and edited from the table.'
              : 'Add the product, then add its first variant from the table.'}
          </DialogDescription>
        </DialogHeader>
        <Form onSubmit={() => !incomplete && save.mutate()}>
          <Field
            label="Name"
            htmlFor="product-name"
            error={nameConflict ? save.error?.message : undefined}
          >
            <Input
              id="product-name"
              required
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          <Field label="Description" htmlFor="product-description">
            <Textarea
              id="product-description"
              maxLength={2000}
              rows={2}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>
          {save.error && !nameConflict && <FormError>{save.error.message}</FormError>}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={props.onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || incomplete}>
              {save.isPending ? 'Saving…' : existing ? 'Save product' : 'Add product'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}

/** Create or edit one variant. `price` is typed in dollars and converted to cents the same
 *  way `ServiceDialog` does (`lib/money.ts`); stock and threshold are whole units. Opening
 *  stock exists only when creating, and goes in as a receipt movement, not a field. */
function VariantDialog(props: {
  product: ProductRow
  variant?: ProductVariantRow
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const existing = props.variant
  const [name, setName] = useState(existing?.name ?? '')
  const [sku, setSku] = useState(existing?.sku ?? '')
  const [barcode, setBarcode] = useState(existing?.barcode ?? '')
  const [price, setPrice] = useState(centsToDollars(existing?.price_cents ?? 0))
  const [quantity, setQuantity] = useState('0')
  const [threshold, setThreshold] = useState(String(existing?.low_stock_threshold ?? 0))
  const [taxKeys, setTaxKeys] = useState<string[]>(existing?.tax_component_keys ?? [])
  const [taxConvention, setTaxConvention] = useState(existing?.tax_convention ?? 'exclusive')

  const cents = dollarsToCents(price)
  const wholeUnit = (value: string) => {
    const parsed = Number(value)
    return Number.isInteger(parsed) && parsed >= 0 ? parsed : null
  }
  const quantityValue = wholeUnit(quantity)
  const thresholdValue = wholeUnit(threshold)

  const problems = {
    price: cents === null ? 'A dollar amount, and never less than nothing.' : undefined,
    quantity:
      !existing && quantityValue === null
        ? 'A whole number, and never less than nothing.'
        : undefined,
    threshold: thresholdValue === null ? 'A whole number, and never less than nothing.' : undefined,
  }
  const incomplete = !name.trim() || !sku.trim() || Object.values(problems).some(Boolean)

  const save = useMutation({
    mutationFn: async () => {
      const draft: ProductVariantDraft = {
        name: name.trim(),
        sku: sku.trim(),
        barcode: barcode.trim() || null,
        price_cents: cents as number,
        low_stock_threshold: thresholdValue as number,
        tax_component_keys: taxKeys,
        tax_convention: taxConvention,
      }
      if (existing) return updateVariant(props.product.id, existing.id, draft)
      const product = await createVariant(props.product.id, draft)
      const created = product.variants.find((v) => v.sku.toLowerCase() === draft.sku.toLowerCase())
      if (!created || !quantityValue) return product
      return receiveStock(product.id, created.id, quantityValue, 'Opening stock')
    },
    onSuccess: () => {
      toast.success(existing ? `Saved ${name.trim()}` : `Added ${name.trim()}`)
      queryClient.invalidateQueries({ queryKey: PRODUCTS })
      props.onClose()
    },
  })

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {existing ? `Edit ${existing.name}` : `Add variant to ${props.product.name}`}
          </DialogTitle>
          <DialogDescription>
            {existing
              ? 'SKU, barcode, price and when to warn about low stock.'
              : 'SKU, barcode, price and the stock you are starting with.'}
          </DialogDescription>
        </DialogHeader>
        <Form onSubmit={() => !incomplete && save.mutate()}>
          <Field label="Name" htmlFor="variant-name" hint="e.g. “500ml”, “Large / Red”">
            <Input
              id="variant-name"
              required
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          <div className="grid grid-cols-2 gap-4">
            <Field label="SKU" htmlFor="variant-sku">
              <Input
                id="variant-sku"
                required
                maxLength={64}
                value={sku}
                onChange={(e) => setSku(e.target.value)}
              />
            </Field>
            <Field label="Barcode" htmlFor="variant-barcode" hint="Optional">
              <Input
                id="variant-barcode"
                maxLength={64}
                value={barcode}
                onChange={(e) => setBarcode(e.target.value)}
              />
            </Field>
          </div>
          <div className={existing ? 'grid grid-cols-2 gap-4' : 'grid grid-cols-3 gap-4'}>
            <Field label="Price" htmlFor="variant-price" error={problems.price} hint="Dollars">
              <Input
                id="variant-price"
                inputMode="decimal"
                className="tabular-nums"
                value={price}
                onChange={(e) => setPrice(e.target.value)}
              />
            </Field>
            {!existing && (
              <Field
                label="Opening stock"
                htmlFor="variant-quantity"
                error={problems.quantity}
                hint="On hand"
              >
                <Input
                  id="variant-quantity"
                  type="number"
                  min={0}
                  step={1}
                  className="tabular-nums"
                  value={quantity}
                  onChange={(e) => setQuantity(e.target.value)}
                />
              </Field>
            )}
            <Field
              label="Low-stock at"
              htmlFor="variant-threshold"
              error={problems.threshold}
              hint="Units"
            >
              <Input
                id="variant-threshold"
                type="number"
                min={0}
                step={1}
                className="tabular-nums"
                value={threshold}
                onChange={(e) => setThreshold(e.target.value)}
              />
            </Field>
          </div>
          <TaxSettingsFields
            idPrefix="variant"
            keys={taxKeys}
            onKeysChange={setTaxKeys}
            convention={taxConvention}
            onConventionChange={setTaxConvention}
          />
          {save.error && <FormError>{save.error.message}</FormError>}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={props.onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || incomplete}>
              {save.isPending ? 'Saving…' : existing ? 'Save variant' : 'Add variant'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}

/** Record a delivery arriving (`inventory.receive`, #100/#61): always adds stock, a note is a
 *  courtesy — matches `ReceiveBody` on the server (`inventory/stock_routes.py`). */
function ReceiveStockDialog(props: {
  product: ProductRow
  variant: ProductVariantRow
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [quantity, setQuantity] = useState('')
  const [note, setNote] = useState('')

  const quantityValue = (() => {
    const parsed = Number(quantity)
    return Number.isInteger(parsed) && parsed >= 1 ? parsed : null
  })()
  const incomplete = quantityValue === null

  const save = useMutation({
    mutationFn: () =>
      receiveStock(props.product.id, props.variant.id, quantityValue as number, note.trim() || null),
    onSuccess: () => {
      toast.success(`Received ${quantityValue} into ${props.variant.name}`)
      queryClient.invalidateQueries({ queryKey: PRODUCTS })
      props.onClose()
    },
  })

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Receive stock — {props.variant.name}</DialogTitle>
          <DialogDescription>Currently {props.variant.quantity_on_hand} on hand.</DialogDescription>
        </DialogHeader>
        <Form onSubmit={() => !incomplete && save.mutate()}>
          <Field
            label="Quantity"
            htmlFor="receive-quantity"
            error={quantity && quantityValue === null ? 'A whole number, at least 1.' : undefined}
          >
            <Input
              id="receive-quantity"
              type="number"
              min={1}
              step={1}
              className="tabular-nums"
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
            />
          </Field>
          <Field label="Note" htmlFor="receive-note" hint="Optional">
            <Input
              id="receive-note"
              maxLength={500}
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </Field>
          {save.error && <FormError>{save.error.message}</FormError>}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={props.onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || incomplete}>
              {save.isPending ? 'Receiving…' : 'Receive stock'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}

/** Correct a stock count (`inventory.adjust`, #100/#61): a person counts the shelf and types
 *  what they counted, not a delta — the difference is computed here and shown the way
 *  counting actually reads ("On hand 12 → counted 9: -3"), and only the signed delta goes to
 *  the server (`AdjustBody`).
 *
 * **Stock is never adjusted blind (spec #95 user story 63).** Before sending, the current
 * on-hand is re-read by refetching the product; if it moved since the dialog opened — a sale,
 * another correction — the difference is recomputed and shown again, and the first submit
 * after that only arms a second confirmation rather than sending anything. */
function AdjustStockDialog(props: {
  product: ProductRow
  variant: ProductVariantRow
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [onHand, setOnHand] = useState(props.variant.quantity_on_hand)
  const [counted, setCounted] = useState(String(props.variant.quantity_on_hand))
  const [reason, setReason] = useState('')
  const [stale, setStale] = useState(false)

  const countedValue = (() => {
    const parsed = Number(counted)
    return Number.isInteger(parsed) && parsed >= 0 ? parsed : null
  })()
  const delta = countedValue === null ? null : countedValue - onHand
  const incomplete = countedValue === null || delta === 0 || !reason.trim()

  // A fresh recheck invalidates whatever the last recheck found stale — editing either field
  // means the confirmation that was about to be sent no longer matches what would be sent.
  const clearStale = () => setStale(false)

  const recheck = useMutation({
    mutationFn: async () => {
      const fresh = await fetchProducts(true)
      const variant = fresh.flatMap((p) => p.variants).find((v) => v.id === props.variant.id)
      return variant?.quantity_on_hand ?? onHand
    },
  })

  const save = useMutation({
    mutationFn: (quantityDelta: number) =>
      adjustStock(props.product.id, props.variant.id, quantityDelta, reason.trim()),
    onSuccess: () => {
      toast.success(`Adjusted ${props.variant.name}`)
      queryClient.invalidateQueries({ queryKey: PRODUCTS })
      props.onClose()
    },
  })

  const handleSubmit = async () => {
    if (incomplete || countedValue === null) return
    if (!stale) {
      const fresh = await recheck.mutateAsync()
      if (fresh !== onHand) {
        setOnHand(fresh)
        setStale(true)
        return
      }
    }
    save.mutate(countedValue - onHand)
  }

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Adjust stock — {props.variant.name}</DialogTitle>
          <DialogDescription>Enter the quantity actually counted.</DialogDescription>
        </DialogHeader>
        <Form onSubmit={handleSubmit}>
          <Field
            label="Counted quantity"
            htmlFor="adjust-counted"
            error={countedValue === null ? 'A whole number, and never less than nothing.' : undefined}
          >
            <Input
              id="adjust-counted"
              type="number"
              min={0}
              step={1}
              className="tabular-nums"
              value={counted}
              onChange={(e) => {
                setCounted(e.target.value)
                clearStale()
              }}
            />
          </Field>
          {countedValue !== null && (
            <p className="text-sm text-muted-foreground">
              On hand {onHand} → counted {countedValue}: {delta! > 0 ? `+${delta}` : delta}
            </p>
          )}
          {stale && (
            <p role="alert" className="text-sm text-warning">
              Stock on hand changed since you opened this dialog — recomputed above. Save
              again to confirm.
            </p>
          )}
          <Field label="Reason" htmlFor="adjust-reason">
            <Textarea
              id="adjust-reason"
              maxLength={500}
              rows={2}
              value={reason}
              onChange={(e) => {
                setReason(e.target.value)
                clearStale()
              }}
            />
          </Field>
          {save.error && <FormError>{save.error.message}</FormError>}
          {recheck.error && <FormError>{recheck.error.message}</FormError>}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={props.onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || recheck.isPending || incomplete}>
              {save.isPending || recheck.isPending
                ? 'Saving…'
                : stale
                  ? 'Confirm and save'
                  : 'Save adjustment'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
