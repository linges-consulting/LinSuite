import { useMutation } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Field, Form, FormError } from '@/components/form'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  recordPayment,
  type PaymentEntry,
  type PaymentMethod,
  type PaymentPayerType,
  type PaymentStatus,
} from '@/lib/api'
import { centsToDollars, dollarsToCents } from '@/lib/money'

export type RecordPaymentDialogProps = {
  invoiceId: string
  kind: 'service' | 'retail'
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The client's outstanding balance — the common case is one click (spec #95 story 21). Read
   *  fresh from the invoice every render, so once a payment lands and the caller's own query
   *  refetches, "Record another" (story 22) offers exactly what is still owed, not what was
   *  owed when the dialog first opened. */
  defaultAmountCents: number
  /** Called once a payment is recorded, so the caller can refetch the invoice/payments list
   *  that `defaultAmountCents` and the ledger table read from. */
  onRecorded?: (payment: PaymentEntry) => void
}

const CLIENT_METHODS: { value: PaymentMethod; label: string }[] = [
  { value: 'cash', label: 'Cash' },
  { value: 'card', label: 'Card' },
  { value: 'e_transfer', label: 'E-transfer' },
]

/**
 * Record payment (#103, spec #95 stories 20-26): the one dialog every screen that takes money
 * opens — here on the invoice view, and reused by Sell (#106) and package purchase (#108)
 * pre-filled right after issuing. Payer **Client** offers cash/card/e-transfer, always records
 * as received, and pre-fills `defaultAmountCents`. Payer **Insurer** fixes the method, offers
 * Approved (pending) or Received, and labels its reference field "Claim #".
 *
 * After a successful save, the dialog stays open showing "Record another" while
 * `defaultAmountCents` (read live from the caller) is still positive — split payments (story
 * 22) are the same dialog, not a second flow.
 */
export function RecordPaymentDialog(props: RecordPaymentDialogProps) {
  const { open, onOpenChange, invoiceId, kind, defaultAmountCents, onRecorded } = props
  const [payerType, setPayerType] = useState<PaymentPayerType>('client')
  const [method, setMethod] = useState<PaymentMethod>('cash')
  const [status, setStatus] = useState<PaymentStatus>('pending')
  const [amount, setAmount] = useState('')
  const [reference, setReference] = useState('')
  const [justRecorded, setJustRecorded] = useState(false)

  // Fresh fields every time the dialog opens — including a "Record another" tap, which closes
  // this step and reopens the form below rather than remounting the dialog.
  useEffect(() => {
    if (!open) return
    setPayerType('client')
    setMethod('cash')
    setStatus('pending')
    setAmount(defaultAmountCents > 0 ? centsToDollars(defaultAmountCents) : '')
    setReference('')
    setJustRecorded(false)
    // Re-arms only when the dialog transitions open, not on every balance refetch while it
    // is already showing the form — the amount field is the person's to edit until they save.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const amountCents = dollarsToCents(amount)
  const effectiveMethod: PaymentMethod = payerType === 'insurer' ? 'insurer' : method
  const effectiveStatus: PaymentStatus = payerType === 'insurer' ? status : 'received'
  const incomplete = amountCents === null || amountCents <= 0

  const save = useMutation({
    mutationFn: () =>
      recordPayment(kind, invoiceId, {
        payer_type: payerType,
        method: effectiveMethod,
        amount_cents: amountCents as number,
        status: effectiveStatus,
        reference: payerType === 'insurer' ? reference.trim() || null : null,
      }),
    onSuccess: (payment) => {
      setJustRecorded(true)
      onRecorded?.(payment)
    },
  })

  const recordAnother = () => {
    setJustRecorded(false)
    setAmount(defaultAmountCents > 0 ? centsToDollars(defaultAmountCents) : '')
    setReference('')
    save.reset()
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Record payment</DialogTitle>
          {!justRecorded && <DialogDescription>How was this paid?</DialogDescription>}
        </DialogHeader>

        {justRecorded ? (
          <div className="flex flex-col gap-4">
            <p className="text-sm">Payment recorded.</p>
            <DialogFooter>
              {defaultAmountCents > 0 && (
                <Button type="button" variant="outline" onClick={recordAnother}>
                  Record another
                </Button>
              )}
              <Button type="button" onClick={() => onOpenChange(false)}>
                Done
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <Form onSubmit={() => !incomplete && save.mutate()}>
            <Field label="Payer" htmlFor="payment-payer">
              <div className="flex gap-1" id="payment-payer">
                <Button
                  type="button"
                  size="sm"
                  variant={payerType === 'client' ? 'default' : 'outline'}
                  aria-pressed={payerType === 'client'}
                  onClick={() => setPayerType('client')}
                >
                  Client
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant={payerType === 'insurer' ? 'default' : 'outline'}
                  aria-pressed={payerType === 'insurer'}
                  onClick={() => setPayerType('insurer')}
                >
                  Insurer
                </Button>
              </div>
            </Field>

            {payerType === 'client' ? (
              <Field label="Method" htmlFor="payment-method">
                <Select value={method} onValueChange={(v) => setMethod(v as PaymentMethod)}>
                  <SelectTrigger id="payment-method">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {CLIENT_METHODS.map((m) => (
                      <SelectItem key={m.value} value={m.value}>
                        {m.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
            ) : (
              <>
                <p className="text-sm text-muted-foreground">Method: Insurer</p>
                <Field label="Status" htmlFor="payment-status">
                  <Select value={status} onValueChange={(v) => setStatus(v as PaymentStatus)}>
                    <SelectTrigger id="payment-status">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="pending">Approved (pending)</SelectItem>
                      <SelectItem value="received">Received</SelectItem>
                    </SelectContent>
                  </Select>
                </Field>
                <Field label="Claim #" htmlFor="payment-reference" hint="Optional">
                  <Input
                    id="payment-reference"
                    value={reference}
                    onChange={(e) => setReference(e.target.value)}
                  />
                </Field>
              </>
            )}

            <Field
              label="Amount"
              htmlFor="payment-amount"
              error={amount && amountCents === null ? 'Enter a dollar amount.' : undefined}
            >
              <Input
                id="payment-amount"
                inputMode="decimal"
                className="tabular-nums"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
              />
            </Field>

            {save.error && <FormError>{save.error.message}</FormError>}

            <DialogFooter>
              <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={incomplete || save.isPending}>
                {save.isPending ? 'Recording…' : 'Record payment'}
              </Button>
            </DialogFooter>
          </Form>
        )}
      </DialogContent>
    </Dialog>
  )
}
