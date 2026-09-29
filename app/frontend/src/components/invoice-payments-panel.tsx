import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CreditCard } from 'lucide-react'
import { useState } from 'react'
import { useSearchParams } from 'react-router'
import { toast } from 'sonner'
import { RecordPaymentDialog } from '@/components/record-payment-dialog'
import { Field, Form, FormError } from '@/components/form'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import {
  correctPayment,
  fetchBalanceExceptions,
  fetchPayments,
  fetchRefunds,
  recordBalanceException,
  recordPayment,
  recordRefund,
  type BalanceException,
  type Invoice,
  type PaymentEntry,
  type RefundEntry,
  type RetailInvoice,
} from '@/lib/api'
import { unsettledPendingInsurerIds } from '@/lib/payments'
import { useCan } from '@/lib/capability-gate'
import { centsToDollars, dollarsToCents, money } from '@/lib/money'
import { INVOICE_BALANCE_EXCEPTIONS, INVOICE_PAYMENTS, INVOICE_REFUNDS } from '@/lib/query-keys'


export type InvoicePaymentsPanelProps = {
  invoice: Invoice | RetailInvoice
  kind: 'service' | 'retail'
  /** Re-reads the invoice — call after a mutation this panel makes lands, so the balance
   *  breakdown above and the lineage links stay in sync with what was just recorded. */
  onRefetch: () => void
}

const METHOD_LABEL: Record<string, string> = {
  cash: 'Cash',
  card: 'Card',
  e_transfer: 'E-transfer',
  insurer: 'Insurer',
}

/**
 * Payments (#103, spec #95 stories 20-29): record a client payment, a split payment, pending
 * insurer approval and its later "mark received", a correction with a reason, and an
 * admin/owner refund or balance exception. Staff-Mode-reachable (`billing.view`, the same
 * capability viewing this invoice already required) — refunds and balance exceptions are the
 * one Admin Mode exception inside this panel, gated by `billing.manage` + Admin Mode (#95's
 * "absent, never disabled" IA rule).
 *
 * **`?pay=1` opens Record payment on arrival.** Sell (#106) and package purchase (#108) land
 * here right after issuing and want the dialog open and pre-filled — a search param the panel
 * reads once, rather than a second navigation-time API, keeps the invoice view itself (#102)
 * untouched. It is stripped from the URL once the dialog closes, so revisiting the same link
 * (back button, refresh) does not reopen it.
 */
export function InvoicePaymentsPanel({ invoice, kind, onRefetch }: InvoicePaymentsPanelProps) {
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [dialogOpen, setDialogOpen] = useState(() => searchParams.get('pay') === '1')
  const [correcting, setCorrecting] = useState<PaymentEntry | null>(null)
  const [refunding, setRefunding] = useState(false)
  const [authorizingException, setAuthorizingException] = useState(false)
  const canManage = useCan('billing.manage')
  const isPackageInvoice = 'package_purchase_id' in invoice && invoice.package_purchase_id !== null

  const paymentsQuery = useQuery({
    queryKey: [...INVOICE_PAYMENTS, kind, invoice.id],
    queryFn: () => fetchPayments(kind, invoice.id),
  })
  const refundsQuery = useQuery({
    queryKey: [...INVOICE_REFUNDS, kind, invoice.id],
    queryFn: () => fetchRefunds(kind, invoice.id),
  })
  const exceptionsQuery = useQuery({
    queryKey: [...INVOICE_BALANCE_EXCEPTIONS, kind, invoice.id],
    queryFn: () => fetchBalanceExceptions(kind, invoice.id),
    enabled: canManage,
  })

  const closeDialog = (open: boolean) => {
    setDialogOpen(open)
    if (!open && searchParams.get('pay') === '1') {
      const next = new URLSearchParams(searchParams)
      next.delete('pay')
      setSearchParams(next, { replace: true })
    }
  }

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: [...INVOICE_PAYMENTS, kind, invoice.id] })
    onRefetch()
  }

  const markReceived = useMutation({
    mutationFn: (payment: PaymentEntry) =>
      recordPayment(kind, invoice.id, {
        payer_type: 'insurer',
        method: 'insurer',
        status: 'received',
        amount_cents: payment.amount_cents,
        reference: payment.reference,
      }),
    onSuccess: () => {
      toast.success('Marked received')
      refresh()
    },
    onError: (error) => toast.error(error.message),
  })

  const payments = paymentsQuery.data?.payments ?? []
  const supersededIds = new Set(
    payments.map((p) => p.corrects_payment_id).filter((id): id is string => id !== null),
  )
  const correctionFor = (paymentId: string) => payments.find((p) => p.corrects_payment_id === paymentId)
  const awaitingInsurer = unsettledPendingInsurerIds(payments, supersededIds)

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <CreditCard className="size-4 text-muted-foreground" aria-hidden />
          Payments
        </CardTitle>
        {invoice.status === 'issued' && (
          <div className="flex gap-2">
            <Button size="sm" onClick={() => closeDialog(true)}>
              Record payment
            </Button>
            {canManage && (
              <>
                {/* A package's money comes back through its own refund (client Packages tab),
                    which voids the credits too; this route refuses package invoices. */}
                {!isPackageInvoice && (
                  <Button size="sm" variant="outline" onClick={() => setRefunding(true)}>
                    Refund
                  </Button>
                )}
                <Button size="sm" variant="outline" onClick={() => setAuthorizingException(true)}>
                  Balance exception
                </Button>
              </>
            )}
          </div>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        {payments.length === 0 ? (
          <p className="text-sm text-muted-foreground">No payments recorded yet.</p>
        ) : (
          <Table aria-label="Payments">
            <TableHeader>
              <TableRow>
                <TableHead>Payer</TableHead>
                <TableHead>Method</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Amount</TableHead>
                <TableHead>Reference</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {payments.map((payment) => {
                const superseded = supersededIds.has(payment.id)
                const correction = superseded ? correctionFor(payment.id) : undefined
                return (
                  <TableRow key={payment.id} id={`payment-${payment.id}`}>
                    <TableCell className={superseded ? 'line-through text-muted-foreground' : ''}>
                      {payment.payer_type === 'insurer' ? 'Insurer' : 'Client'}
                    </TableCell>
                    <TableCell className={superseded ? 'line-through text-muted-foreground' : ''}>
                      {METHOD_LABEL[payment.method] ?? payment.method}
                    </TableCell>
                    <TableCell className={superseded ? 'line-through text-muted-foreground' : ''}>
                      {payment.status === 'pending' ? (
                        <Badge variant="warning">Pending</Badge>
                      ) : (
                        <Badge variant="success">Received</Badge>
                      )}
                    </TableCell>
                    <TableCell
                      className={`text-right tabular-nums ${superseded ? 'line-through text-muted-foreground' : ''}`}
                    >
                      {money(payment.amount_cents)}
                    </TableCell>
                    <TableCell className={superseded ? 'line-through text-muted-foreground' : ''}>
                      {payment.reference ?? '—'}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex flex-col items-end gap-1">
                        {payment.corrects_payment_id && (
                          <a
                            href={`#payment-${payment.corrects_payment_id}`}
                            className="text-xs text-primary hover:underline"
                          >
                            Corrects an earlier entry
                          </a>
                        )}
                        {superseded && correction && (
                          <a
                            href={`#payment-${correction.id}`}
                            className="text-xs text-primary hover:underline"
                          >
                            Superseded by correction
                          </a>
                        )}
                        {!superseded && invoice.status === 'issued' && (
                          <div className="flex gap-2">
                            {payment.payer_type === 'insurer' &&
                              payment.status === 'pending' &&
                              awaitingInsurer.has(payment.id) && (
                              <Button
                                size="sm"
                                variant="outline"
                                disabled={markReceived.isPending}
                                onClick={() => markReceived.mutate(payment)}
                              >
                                Mark received
                              </Button>
                            )}
                            <Button size="sm" variant="ghost" onClick={() => setCorrecting(payment)}>
                              Correct
                            </Button>
                          </div>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}

        {(refundsQuery.data?.refunds.length ?? 0) > 0 && (
          <RefundsList refunds={refundsQuery.data!.refunds} />
        )}
        {canManage && (exceptionsQuery.data?.exceptions.length ?? 0) > 0 && (
          <ExceptionsList exceptions={exceptionsQuery.data!.exceptions} />
        )}
      </CardContent>

      <RecordPaymentDialog
        invoiceId={invoice.id}
        kind={kind}
        open={dialogOpen}
        onOpenChange={closeDialog}
        defaultAmountCents={Math.max(invoice.client_outstanding_cents, 0)}
        onRecorded={refresh}
      />
      {correcting && (
        <CorrectPaymentDialog
          invoiceId={invoice.id}
          kind={kind}
          payment={correcting}
          onClose={() => setCorrecting(null)}
          onCorrected={refresh}
        />
      )}
      {refunding && (
        <RefundDialog
          invoiceId={invoice.id}
          kind={kind}
          onClose={() => setRefunding(false)}
          onRefunded={() => {
            queryClient.invalidateQueries({ queryKey: [...INVOICE_REFUNDS, kind, invoice.id] })
            onRefetch()
          }}
        />
      )}
      {authorizingException && (
        <BalanceExceptionDialog
          invoiceId={invoice.id}
          kind={kind}
          onClose={() => setAuthorizingException(false)}
          onAuthorized={() => {
            queryClient.invalidateQueries({
              queryKey: [...INVOICE_BALANCE_EXCEPTIONS, kind, invoice.id],
            })
            onRefetch()
          }}
        />
      )}
    </Card>
  )
}

function RefundsList({ refunds }: { refunds: RefundEntry[] }) {
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">Refunds</p>
      {refunds.map((refund) => (
        <div key={refund.id} className="flex justify-between text-sm">
          <span className="text-muted-foreground">{refund.reason}</span>
          <span className="tabular-nums">{money(refund.amount_cents)}</span>
        </div>
      ))}
    </div>
  )
}

function ExceptionsList({ exceptions }: { exceptions: BalanceException[] }) {
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">Balance exceptions</p>
      {exceptions.map((exception) => (
        <div key={exception.id} className="flex justify-between text-sm">
          <span className="text-muted-foreground">{exception.reason}</span>
          <span className="tabular-nums">{money(exception.outstanding_cents_at_authorization)}</span>
        </div>
      ))}
    </div>
  )
}

/** Correct an entry (#103, spec #95 story 26): a required reason, and an amount that must
 *  actually change — the server refuses a correction that changes nothing
 *  (`billing/payments.py::_correct_payment`), so the form enforces that before it ever asks. */
function CorrectPaymentDialog(props: {
  invoiceId: string
  kind: 'service' | 'retail'
  payment: PaymentEntry
  onClose: () => void
  onCorrected: () => void
}) {
  const { payment } = props
  const [amount, setAmount] = useState(centsToDollars(payment.amount_cents))
  const [reason, setReason] = useState('')

  const amountCents = dollarsToCents(amount)
  const incomplete =
    amountCents === null || amountCents <= 0 || amountCents === payment.amount_cents || !reason.trim()

  const save = useMutation({
    mutationFn: () =>
      correctPayment(props.kind, props.invoiceId, payment.id, {
        reason: reason.trim(),
        amount_cents: amountCents as number,
      }),
    onSuccess: () => {
      toast.success('Correction recorded')
      props.onCorrected()
      props.onClose()
    },
  })

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Correct payment</DialogTitle>
        </DialogHeader>
        <Form onSubmit={() => !incomplete && save.mutate()}>
          <Field
            label="Amount"
            htmlFor="correct-amount"
            error={amountCents === payment.amount_cents ? 'Change the amount to correct it.' : undefined}
          >
            <Input
              id="correct-amount"
              inputMode="decimal"
              className="tabular-nums"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
            />
          </Field>
          <Field label="Reason" htmlFor="correct-reason">
            <Textarea
              id="correct-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
          {save.error && <FormError>{save.error.message}</FormError>}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={props.onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={incomplete || save.isPending}>
              {save.isPending ? 'Saving…' : 'Save correction'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}

/** Refund (#103, spec #95 story 28): `billing.manage`, Admin Mode — the server caps it at
 *  money actually received, across the invoice's replacement lineage; a refusal past the cap
 *  renders here rather than being pre-computed on the client. */
function RefundDialog(props: {
  invoiceId: string
  kind: 'service' | 'retail'
  onClose: () => void
  onRefunded: () => void
}) {
  const [amount, setAmount] = useState('')
  const [reason, setReason] = useState('')
  const amountCents = dollarsToCents(amount)
  const incomplete = amountCents === null || amountCents <= 0 || !reason.trim()

  const save = useMutation({
    mutationFn: () =>
      recordRefund(props.kind, props.invoiceId, { amount_cents: amountCents as number, reason: reason.trim() }),
    onSuccess: () => {
      toast.success('Refund recorded')
      props.onRefunded()
      props.onClose()
    },
  })

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Refund</DialogTitle>
        </DialogHeader>
        <Form onSubmit={() => !incomplete && save.mutate()}>
          <Field label="Amount" htmlFor="refund-amount">
            <Input
              id="refund-amount"
              inputMode="decimal"
              className="tabular-nums"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
            />
          </Field>
          <Field label="Reason" htmlFor="refund-reason">
            <Textarea id="refund-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
          {save.error && <FormError>{save.error.message}</FormError>}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={props.onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={incomplete || save.isPending}>
              {save.isPending ? 'Refunding…' : 'Record refund'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}

/** Balance exception (#103, spec #95 story 29): `billing.manage`, Admin Mode — closes an
 *  invoice the business chooses not to collect, without pretending it was paid. */
function BalanceExceptionDialog(props: {
  invoiceId: string
  kind: 'service' | 'retail'
  onClose: () => void
  onAuthorized: () => void
}) {
  const [reason, setReason] = useState('')
  const incomplete = !reason.trim()

  const save = useMutation({
    mutationFn: () => recordBalanceException(props.kind, props.invoiceId, { reason: reason.trim() }),
    onSuccess: () => {
      toast.success('Balance exception recorded')
      props.onAuthorized()
      props.onClose()
    },
  })

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Balance exception</DialogTitle>
        </DialogHeader>
        <Form onSubmit={() => !incomplete && save.mutate()}>
          <Field label="Reason" htmlFor="exception-reason">
            <Textarea
              id="exception-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
          {save.error && <FormError>{save.error.message}</FormError>}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={props.onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={incomplete || save.isPending}>
              {save.isPending ? 'Saving…' : 'Record exception'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
