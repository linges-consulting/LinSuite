import { useMutation } from '@tanstack/react-query'
import { Undo2 } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { Field, Form, FormError } from '@/components/form'
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
import { Textarea } from '@/components/ui/textarea'
import { returnRetailItems, type RetailInvoice, type RetailInvoiceLine } from '@/lib/api'
import { useCan } from '@/lib/capability-gate'
import { centsToDollars, dollarsToCents } from '@/lib/money'

export type InvoiceReturnsPanelProps = {
  invoice: RetailInvoice
  /** Re-reads the invoice once a return lands, so restocked quantities and the refund the
   *  return may carry show up in the balance breakdown above without a manual reload. */
  onRefetch: () => void
}

/**
 * Retail returns (#105, spec #95 story 50): choose lines and quantities, restock or not per
 * line, and an optional refund capped at money received. Admin Mode (`billing.manage`) — the
 * same gate #95's IA decision draws around it — rendered only through that gate (#95: "absent
 * in Staff Mode ... never disabled"), so it never appears at all for a service invoice, which
 * has no such thing.
 *
 * A line already returned in full (`quantity - returned_quantity == 0`, #105's own backend
 * addition to `RetailInvoiceOut`) never appears in the dialog — the least-invasive way to keep
 * a quantity from ever exceeding what remains returnable, matching the server's own per-line
 * cap (`billing/retail_sales.py::return_retail_items`).
 */
export function InvoiceReturnsPanel({ invoice, onRefetch }: InvoiceReturnsPanelProps) {
  const canReturn = useCan('billing.manage')
  const [open, setOpen] = useState(false)
  if (!canReturn) return null

  const returnable = invoice.lines.filter((line) => line.quantity > line.returned_quantity)

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Undo2 className="size-4 text-muted-foreground" aria-hidden />
          Returns
        </CardTitle>
      </CardHeader>
      <CardContent>
        {invoice.status !== 'issued' ? (
          <p className="text-sm text-muted-foreground">
            This invoice is cancelled — nothing left to return.
          </p>
        ) : returnable.length === 0 ? (
          <p className="text-sm text-muted-foreground">Every line has already been returned.</p>
        ) : (
          <Button variant="outline" onClick={() => setOpen(true)}>
            Return items
          </Button>
        )}
      </CardContent>
      {open && (
        <ReturnItemsDialog
          invoice={invoice}
          lines={returnable}
          onClose={() => setOpen(false)}
          onSuccess={() => {
            setOpen(false)
            onRefetch()
          }}
        />
      )}
    </Card>
  )
}

/** A whole number, clamped to `[0, max]` — the one place a typed or over-typed quantity is
 *  ever turned into what gets sent, so the displayed value and the request body always agree
 *  and neither can carry a line past what remains returnable. */
function clampQuantity(raw: string, max: number): number {
  const parsed = Number(raw)
  if (!Number.isInteger(parsed) || parsed < 0) return 0
  return Math.min(parsed, Math.max(max, 0))
}

function ReturnItemsDialog(props: {
  invoice: RetailInvoice
  lines: RetailInvoiceLine[]
  onClose: () => void
  onSuccess: () => void
}) {
  const { invoice, lines } = props
  const [quantities, setQuantities] = useState<Record<string, number>>(
    Object.fromEntries(lines.map((line) => [line.id, 0])),
  )
  const [restock, setRestock] = useState<Record<string, boolean>>(
    Object.fromEntries(lines.map((line) => [line.id, true])),
  )
  const [reason, setReason] = useState('')
  const [refund, setRefund] = useState('')

  // Refunds are capped at money received net of prior refunds (`billing/payments.py::Balance`
  // docstring) — with no lineage or prepaid credit on a retail invoice, that is exactly
  // `grand_total_cents - outstanding_cents`, already on the wire, so no extra read is needed
  // to show the cap before the server would enforce it.
  const maxRefundCents = Math.max(0, invoice.grand_total_cents - invoice.outstanding_cents)
  const refundTyped = refund.trim() !== ''
  const refundCents = refundTyped ? dollarsToCents(refund) : null
  const refundInvalid = refundTyped && (refundCents === null || refundCents <= 0 || refundCents > maxRefundCents)

  const selectedLines = lines
    .map((line) => ({ line, quantity: quantities[line.id] ?? 0 }))
    .filter((s) => s.quantity > 0)
  const incomplete = selectedLines.length === 0 || !reason.trim() || refundInvalid

  const save = useMutation({
    mutationFn: () =>
      returnRetailItems(invoice.id, {
        reason: reason.trim(),
        lines: selectedLines.map((s) => ({
          retail_invoice_line_id: s.line.id,
          quantity: s.quantity,
          restock: restock[s.line.id] ?? true,
        })),
        ...(refundCents ? { refund_cents: refundCents } : {}),
      }),
    onSuccess: (result) => {
      toast.success(
        result.refund_cents
          ? `Return recorded — refunded $${centsToDollars(result.refund_cents)}`
          : 'Return recorded',
      )
      props.onSuccess()
    },
  })

  return (
    <Dialog open onOpenChange={(next) => !next && props.onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Return items</DialogTitle>
          <DialogDescription>
            Choose what came back and whether it goes back on the shelf.
          </DialogDescription>
        </DialogHeader>
        <Form onSubmit={() => !incomplete && save.mutate()}>
          <div className="flex flex-col gap-3">
            {lines.map((line) => {
              const remaining = line.quantity - line.returned_quantity
              return (
                <div key={line.id} className="rounded-lg border p-3">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <p className="text-sm font-medium">{line.variant_name}</p>
                      <p className="text-xs text-muted-foreground">
                        Sold {line.quantity} · {remaining} left to return
                      </p>
                    </div>
                    <div className="w-20">
                      <Label htmlFor={`return-qty-${line.id}`} className="text-xs">
                        Qty
                      </Label>
                      <Input
                        id={`return-qty-${line.id}`}
                        type="number"
                        min={0}
                        max={remaining}
                        step={1}
                        className="tabular-nums"
                        value={quantities[line.id]}
                        onChange={(e) =>
                          setQuantities((q) => ({
                            ...q,
                            [line.id]: clampQuantity(e.target.value, remaining),
                          }))
                        }
                      />
                    </div>
                  </div>
                  <label className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
                    <Checkbox
                      checked={restock[line.id] ?? true}
                      onCheckedChange={(on) =>
                        setRestock((r) => ({ ...r, [line.id]: on === true }))
                      }
                    />
                    Restock
                  </label>
                </div>
              )
            })}
          </div>

          <Field label="Reason" htmlFor="return-reason">
            <Textarea
              id="return-reason"
              maxLength={2000}
              rows={2}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>

          <Field
            label="Refund"
            htmlFor="return-refund"
            hint={`Optional — up to $${centsToDollars(maxRefundCents)} received`}
            error={
              refundInvalid ? `A dollar amount, up to $${centsToDollars(maxRefundCents)}.` : undefined
            }
          >
            <Input
              id="return-refund"
              inputMode="decimal"
              className="tabular-nums"
              placeholder="0.00"
              value={refund}
              onChange={(e) => setRefund(e.target.value)}
            />
          </Field>

          {save.error && <FormError>{save.error.message}</FormError>}

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={props.onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || incomplete}>
              {save.isPending ? 'Returning…' : 'Return items'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
