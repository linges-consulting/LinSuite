import { useMutation } from '@tanstack/react-query'
import { Ban } from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import { Field, Form, FormError } from '@/components/form'
import type { ReplacesInvoiceState } from '@/components/replaces-invoice-banner'
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
import { Textarea } from '@/components/ui/textarea'
import {
  cancelInvoice,
  cancelRetailInvoice,
  type CancelledInvoiceOut,
  type CancelledRetailInvoiceOut,
  type Invoice,
  type RetailInvoice,
} from '@/lib/api'
import { useCan } from '@/lib/capability-gate'

export type InvoiceCancelPanelProps = {
  invoice: Invoice | RetailInvoice
  kind: 'service' | 'retail'
  /** Cancelling opens a replacement draft elsewhere (bill review for a service invoice, Sell
   *  for a retail one, #95's own IA decision) rather than staying on this screen, so #107 uses
   *  this to invalidate the cached detail before it navigates away. */
  onRefetch: () => void
}

/**
 * Cancel & replace (#107, spec #95 stories 30-33): a required reason, then straight to the
 * replacement draft this invoice's own bill/sale reopens as (`replacement_bill_id`/
 * `replacement_sale_id`, `billing/invoices.py::CancelledOut`/`billing/retail_sales.py::
 * CancelledRetailOut`). Admin Mode (`billing.manage`) — #95's IA decision — rendered only
 * through that gate, and absent once an invoice is already cancelled (there is nothing left
 * to void).
 *
 * This file is that ticket's own slot, kept out of `invoice-view.tsx` so #107 lands here
 * without touching the invoice view, the lineage cards, or any sibling action slot.
 */
export function InvoiceCancelPanel({ invoice, kind, onRefetch }: InvoiceCancelPanelProps) {
  const canCancel = useCan('billing.manage')
  const [open, setOpen] = useState(false)
  if (!canCancel || invoice.status === 'cancelled') return null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Ban className="size-4 text-muted-foreground" aria-hidden />
          Cancel &amp; replace
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="mb-3 text-sm text-muted-foreground">
          Voids this invoice and reopens it as a draft to fix and reissue. The original is kept
          on record, never deleted.
        </p>
        <Button variant="outline" onClick={() => setOpen(true)}>
          Cancel
        </Button>
      </CardContent>
      {open && (
        <CancelDialog
          invoice={invoice}
          kind={kind}
          onClose={() => setOpen(false)}
          onCancelled={onRefetch}
        />
      )}
    </Card>
  )
}

function CancelDialog({
  invoice,
  kind,
  onClose,
  onCancelled,
}: {
  invoice: Invoice | RetailInvoice
  kind: 'service' | 'retail'
  onClose: () => void
  onCancelled: () => void
}) {
  const navigate = useNavigate()
  const [reason, setReason] = useState('')
  const incomplete = reason.trim() === ''

  const cancel = useMutation<CancelledInvoiceOut | CancelledRetailInvoiceOut, Error>({
    mutationFn: (): Promise<CancelledInvoiceOut | CancelledRetailInvoiceOut> =>
      kind === 'retail'
        ? cancelRetailInvoice(invoice.id, reason.trim())
        : cancelInvoice(invoice.id, reason.trim()),
    onSuccess: (result) => {
      onCancelled()
      const replacesInvoice: ReplacesInvoiceState = {
        number: invoice.invoice_number,
        reason: reason.trim(),
      }
      if ('replacement_bill_id' in result) {
        navigate(`/bills/${result.replacement_bill_id}`, { state: { replacesInvoice } })
      } else {
        navigate(`/sell?sale=${result.replacement_sale_id}`, { state: { replacesInvoice } })
      }
    },
  })

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Cancel &amp; replace invoice #{invoice.invoice_number}</DialogTitle>
          <DialogDescription>
            The original is kept on record, marked cancelled. A new draft opens with its lines
            carried over so you can fix it and reissue.
          </DialogDescription>
        </DialogHeader>
        <Form onSubmit={() => !incomplete && cancel.mutate()}>
          <Field label="Reason" htmlFor="cancel-reason">
            <Textarea
              id="cancel-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              required
              autoFocus
            />
          </Field>
          {cancel.isError && (
            <FormError>
              {cancel.error instanceof Error ? cancel.error.message : 'Could not cancel this invoice'}
            </FormError>
          )}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>
              Keep invoice
            </Button>
            <Button type="submit" variant="destructive" disabled={cancel.isPending || incomplete}>
              {cancel.isPending ? 'Cancelling…' : 'Cancel & replace'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
