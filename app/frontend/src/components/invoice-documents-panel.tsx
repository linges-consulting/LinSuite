import { useEffect, useRef, useState } from 'react'
import { FileText, Mail, Printer } from 'lucide-react'
import { toast } from 'sonner'
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
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  ApiError,
  type EmailedOut,
  type Invoice,
  type InvoiceLine,
  type RetailInvoice,
  emailInvoice,
  emailRetailInvoice,
  emailTreatmentReceipt,
  invoicePdfUrl,
  pollDocument,
  receiptPdfUrl,
  retailInvoicePdfUrl,
} from '@/lib/api'

export type InvoiceDocumentsPanelProps = {
  invoice: Invoice | RetailInvoice
  kind: 'service' | 'retail'
  /** Not needed to render a print/email/receipt action today, but every action slot on this
   *  screen takes it (#102's own contract) — called once a queued email is confirmed. */
  onRefetch: () => void
}

const POLL_INTERVAL_MS = 2000
const POLL_TIMEOUT_MS = 30_000
/** The exact string `billing/invoices.py::_customer_email` raises — matched, not a generic
 *  catch-all, so a different 422 (an unverified sender, say) still reads as a real failure
 *  rather than silently dropping into "type an address". */
const NO_EMAIL_ON_FILE = 'This client has no email address on file.'

type PrintState = 'idle' | 'preparing' | 'timeout'

/**
 * Print one document: poll a 202 print route every two seconds for up to thirty, then hand the
 * rendered bytes to an already-open tab.
 *
 * The tab opens synchronously inside the click handler, before the first poll — the same fix
 * `client-forms.tsx::openPdf` already uses ("Safari blocks a new window after an awaited
 * fetch"). Every following render (a retry, or the eventual 200) just points that same tab at
 * a same-origin blob URL built from the bytes this fetch already has in hand, rather than a
 * second unauthenticated navigation that would have to rediscover the 202/200 distinction on
 * its own.
 */
function usePrintButton(url: string) {
  const [state, setState] = useState<PrintState>('idle')
  const runRef = useRef(0)

  useEffect(() => () => {
    runRef.current += 1
  }, [])

  async function print() {
    const mine = ++runRef.current
    const tab = window.open('', '_blank')
    if (!tab) {
      toast.error('Allow pop-ups to print this document.')
      return
    }
    tab.opener = null
    setState('preparing')
    const deadline = Date.now() + POLL_TIMEOUT_MS
    while (runRef.current === mine) {
      let result
      try {
        result = await pollDocument(url)
      } catch (error) {
        tab.close()
        setState('idle')
        toast.error(error instanceof Error ? error.message : 'Could not open the document')
        return
      }
      if (runRef.current !== mine) return
      if (result.ready) {
        const objectUrl = URL.createObjectURL(result.blob)
        tab.location.replace(objectUrl)
        window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000)
        setState('idle')
        return
      }
      if (Date.now() >= deadline) {
        setState('timeout')
        return
      }
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS))
    }
  }

  const label =
    state === 'preparing' ? 'Preparing…' : state === 'timeout' ? 'Still preparing — try again in a moment' : 'Print'
  return { label, busy: state === 'preparing', onClick: print }
}

function PrintButton({ url, label }: { url: string; label: string }) {
  const button = usePrintButton(url)
  return (
    <Button variant="outline" size="sm" onClick={button.onClick} disabled={button.busy}>
      <Printer aria-hidden />
      {button.label === 'Print' ? label : button.label}
    </Button>
  )
}

/**
 * Email one document: confirm the client's address on file with no round trip that reveals it
 * (the invoice payload never carries it — only the server does); a 422 for
 * `NO_EMAIL_ON_FILE` switches the same dialog to a typed address instead of a dead end. An
 * anonymous sale has no address to confirm, so it opens straight into that typed mode.
 */
function useEmailDialog(send: (to?: string) => Promise<EmailedOut>, hasClientOnFile: boolean, onSent: () => void) {
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState(false)
  const [address, setAddress] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)

  function openDialog() {
    setTyped(!hasClientOnFile)
    setAddress('')
    setError(null)
    setOpen(true)
  }

  async function submit() {
    setSending(true)
    setError(null)
    try {
      const result = await send(typed ? address : undefined)
      toast.success(`Email queued to ${result.to}`)
      setOpen(false)
      onSent()
    } catch (err) {
      if (!typed && err instanceof ApiError && err.status === 422 && err.message === NO_EMAIL_ON_FILE) {
        setTyped(true)
      } else {
        setError(err instanceof Error ? err.message : 'Could not send the email')
      }
    } finally {
      setSending(false)
    }
  }

  return { open, setOpen, typed, address, setAddress, error, sending, openDialog, submit }
}

function EmailDialogButton({
  label,
  title,
  send,
  hasClientOnFile,
  onSent,
}: {
  label: string
  title: string
  send: (to?: string) => Promise<EmailedOut>
  hasClientOnFile: boolean
  onSent: () => void
}) {
  const dialog = useEmailDialog(send, hasClientOnFile, onSent)
  const inputId = `${title.replace(/\s+/g, '-').toLowerCase()}-email-address`
  return (
    <>
      <Button variant="outline" size="sm" onClick={dialog.openDialog}>
        <Mail aria-hidden />
        {label}
      </Button>
      <Dialog open={dialog.open} onOpenChange={dialog.setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>
              {dialog.typed
                ? hasClientOnFile
                  ? 'This client has no email address on file. Enter one to send to.'
                  : 'Enter an email address to send to.'
                : "This will be sent to the client's email address on file."}
            </DialogDescription>
          </DialogHeader>
          {dialog.typed && (
            <div className="space-y-1.5">
              <Label htmlFor={inputId}>Email address</Label>
              <Input
                id={inputId}
                type="email"
                value={dialog.address}
                onChange={(event) => dialog.setAddress(event.target.value)}
                placeholder="client@example.com"
                autoFocus
              />
            </div>
          )}
          {dialog.error && (
            <p role="alert" className="text-sm text-destructive">
              {dialog.error}
            </p>
          )}
          <DialogFooter>
            <Button
              onClick={dialog.submit}
              disabled={dialog.sending || (dialog.typed && dialog.address.trim() === '')}
            >
              Send
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

/** Released once the invoice's checkout is complete (`billing/documents.py::receipt_status`:
 *  `invoice.status == "issued" and balance.checkout_complete`) — the same figure already on
 *  every invoice payload, never a separate per-line flag. */
function receiptReleased(invoice: Invoice): boolean {
  return invoice.status === 'issued' && invoice.checkout_complete
}

function TreatmentReceiptRow({ invoice, line }: { invoice: Invoice; line: InvoiceLine }) {
  const released = receiptReleased(invoice)
  return (
    // `role="group"` names the row by its service — the same "Swedish Massage" text this
    // line's own row in the invoice's lines table already shows above, so plain text queries
    // are ambiguous and a test (or a screen reader) scopes by this group instead.
    <div
      role="group"
      aria-label={line.service_name}
      className="flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-sm"
    >
      <span>{line.service_name}</span>
      {released ? (
        <div className="flex items-center gap-2">
          <PrintButton url={receiptPdfUrl(invoice.customer_id, invoice.id, line.id)} label="Print" />
          <EmailDialogButton
            label="Email"
            title={`Email receipt — ${line.service_name}`}
            hasClientOnFile
            send={(to) => emailTreatmentReceipt(invoice.customer_id, invoice.id, line.id, to)}
            onSent={() => {}}
          />
        </div>
      ) : (
        <span className="text-muted-foreground">Receipt available after checkout</span>
      )}
    </div>
  )
}

/**
 * Print, email and treatment receipts (#104, spec #95 stories 34-41): open the invoice PDF
 * (polling a 202 while it renders), email it to the client's address on file or a typed one
 * for a walk-in, and a per-service-line treatment receipt once checkout releases it.
 * Staff-Mode-reachable (`billing.view`), same as the rest of this screen.
 */
export function InvoiceDocumentsPanel({ invoice, kind, onRefetch }: InvoiceDocumentsPanelProps) {
  const isService = kind === 'service'
  const serviceInvoice = isService ? (invoice as Invoice) : null
  const retailInvoice = !isService ? (invoice as RetailInvoice) : null

  const printUrl = isService
    ? invoicePdfUrl(serviceInvoice!.customer_id, serviceInvoice!.id)
    : retailInvoicePdfUrl(retailInvoice!.id, retailInvoice!.customer_id)
  const documentLabel = isService ? 'invoice' : 'retail invoice'
  const emailSend = (to?: string) =>
    isService
      ? emailInvoice(serviceInvoice!.customer_id, serviceInvoice!.id, to)
      : emailRetailInvoice(retailInvoice!.id, retailInvoice!.customer_id, to)
  const hasClientOnFile = isService ? true : retailInvoice!.customer_id !== null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FileText className="size-4 text-muted-foreground" aria-hidden />
          Print, email &amp; receipts
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-center gap-2">
          <PrintButton url={printUrl} label={`Print ${documentLabel}`} />
          <EmailDialogButton
            label={`Email ${documentLabel}`}
            title={`Email ${documentLabel}`}
            hasClientOnFile={hasClientOnFile}
            send={emailSend}
            onSent={onRefetch}
          />
        </div>

        {serviceInvoice && serviceInvoice.lines.length > 0 && (
          <div className="space-y-1.5">
            <p className="text-sm font-medium">Treatment receipts</p>
            <div className="space-y-1.5">
              {serviceInvoice.lines.map((line) => (
                <TreatmentReceiptRow key={line.id} invoice={serviceInvoice} line={line} />
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
