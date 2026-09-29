import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { RecordPaymentDialog } from '@/components/record-payment-dialog'
import { createQueryClient } from '@/lib/query-client'
import { renderApp, stubApi } from './harness'

/**
 * Payments on the invoice view (#103, spec #95 stories 20-29): record a client payment with
 * the balance pre-fill, split payments via "Record another", insurer pending/received and its
 * "mark received", a required-reason correction that supersedes the original, and refunds /
 * balance exceptions gated to Admin Mode + `billing.manage`.
 */

afterEach(() => vi.unstubAllGlobals())

const ACCOUNT = (capabilities: string[], mode: 'staff' | 'admin' = 'staff') => ({
  id: 'u2',
  email: 'desk@cedar.example',
  role: 'Staff',
  capabilities,
  mode,
  can_switch_modes: mode === 'admin',
  admin_grant_expires_at: mode === 'admin' ? new Date(Date.now() + 900_000).toISOString() : null,
  admin_hard_limit_at: mode === 'admin' ? new Date(Date.now() + 900_000).toISOString() : null,
  must_change_password: false,
  mfa: {
    enrolled: false,
    method: null,
    pending: false,
    enrolment_required: false,
    verified_at: null,
    email_otp_allowed: false,
  },
})

const BALANCE = {
  outstanding_cents: 5000,
  pending_insurer_cents: 0,
  client_outstanding_cents: 5000,
  checkout_complete: false,
  refunded_cents: 0,
  prepaid_cents: 0,
  held_credit_cents: 0,
}

function serviceInvoice(overrides: Record<string, unknown> = {}) {
  return {
    id: 'inv1',
    invoice_number: 42,
    service_bill_id: 'b1',
    package_purchase_id: null,
    customer_id: 'c1',
    customer_name: 'Priya Nair',
    status: 'issued' as const,
    computed_subtotal_cents: 5000,
    computed_discount_total_cents: 0,
    computed_tax_total_cents: 0,
    computed_grand_total_cents: 5000,
    tax_totals_by_component: {},
    tax_rates_by_component: {},
    override_applied_cents: null,
    override_reason: null,
    override_tax_convention: null,
    grand_total_cents: 5000,
    issued_at: '2026-09-20T15:00:00Z',
    issued_by: 'u1',
    replaces_invoice_id: null,
    replaced_by_invoice_id: null,
    cancelled_at: null,
    cancel_reason: null,
    lines: [
      {
        id: 'l1',
        appointment_id: 'a1',
        service_id: 'sv1',
        service_name: 'Swedish Massage',
        staff_id: 'st1',
        staff_name: 'Ana Rossi',
        price_cents: 5000,
        discounted_cents: 5000,
        pretax_cents: 5000,
        tax_cents: 0,
        line_total_cents: 5000,
        prepaid_cents: 0,
        tax_convention: 'exclusive' as const,
        override_adjustment_cents: 0,
        discounts: [],
        taxes: [],
      },
    ],
    ...BALANCE,
    ...overrides,
  }
}

/** A minimal in-memory ledger, so `record payment`/`mark received`/`correct` behave the way
 *  `billing/payments.py` does closely enough for these tests: superseding, and the balance
 *  figures the invoice re-read after a mutation shows. */
function paymentsServer(invoice: ReturnType<typeof serviceInvoice>) {
  let current = invoice
  const payments: any[] = []
  let nextId = 1

  const recompute = () => {
    const received = payments
      .filter((p) => p.status === 'received' && !payments.some((c) => c.corrects_payment_id === p.id))
      .reduce((sum, p) => sum + p.amount_cents, 0)
    const pending = payments
      .filter((p) => p.status === 'pending' && !payments.some((c) => c.corrects_payment_id === p.id))
      .reduce((sum, p) => sum + p.amount_cents, 0)
    const receivedInsurer = payments
      .filter(
        (p) =>
          p.status === 'received' &&
          p.payer_type === 'insurer' &&
          !payments.some((c) => c.corrects_payment_id === p.id),
      )
      .reduce((sum, p) => sum + p.amount_cents, 0)
    const outstanding = current.grand_total_cents - received
    const pendingInsurer = Math.max(pending - receivedInsurer, 0)
    current = {
      ...current,
      outstanding_cents: outstanding,
      pending_insurer_cents: pendingInsurer,
      client_outstanding_cents: outstanding - pendingInsurer,
      checkout_complete: outstanding - pendingInsurer <= 0,
    }
  }

  return {
    get invoice() {
      return current
    },
    // GET calls never carry a body (`fetch(url, { cache: 'no-store' })`); every write here
    // does (`send()` always JSON-stringifies its payload) — so `body === undefined` is exactly
    // "this was a read", with no need to also plumb the HTTP method through `stubApi`'s
    // `respond` signature.
    respond: (url: string, body: any): Response | undefined => {
      if (url === '/api/invoices/inv1' && body === undefined) return Response.json(current)
      if (url === '/api/invoices/inv1/payments' && body === undefined) {
        return Response.json({ payments, transfers: [] })
      }
      if (url === '/api/invoices/inv1/payments' && body !== undefined) {
        const payment = {
          id: `p${nextId++}`,
          invoice_id: 'inv1',
          retail_invoice_id: null,
          payer_type: body.payer_type,
          method: body.method,
          status: body.status ?? 'received',
          amount_cents: body.amount_cents,
          reference: body.reference ?? null,
          collected_by: 'u2',
          recorded_at: new Date().toISOString(),
          corrects_payment_id: null,
          correction_reason: null,
        }
        payments.push(payment)
        recompute()
        return Response.json(payment, { status: 201 })
      }
      const correctMatch = url.match(/^\/api\/invoices\/inv1\/payments\/([^/]+)\/corrections$/)
      if (correctMatch && body !== undefined) {
        const original = payments.find((p) => p.id === correctMatch[1])
        const correction = {
          ...original,
          id: `p${nextId++}`,
          amount_cents: body.amount_cents ?? original.amount_cents,
          reference: body.reference ?? original.reference,
          corrects_payment_id: original.id,
          correction_reason: body.reason,
          recorded_at: new Date().toISOString(),
        }
        payments.push(correction)
        recompute()
        return Response.json(correction, { status: 201 })
      }
      if (url === '/api/invoices/inv1/refunds' && body === undefined) {
        return Response.json({ refunds: [] })
      }
      if (url === '/api/invoices/inv1/refunds' && body !== undefined) {
        return Response.json(
          { id: 'r1', invoice_id: 'inv1', retail_invoice_id: null, ...body, approved_by: 'u2', refunded_at: new Date().toISOString() },
          { status: 201 },
        )
      }
      if (url === '/api/invoices/inv1/balance-exceptions' && body === undefined) {
        return Response.json({ exceptions: [] })
      }
      if (url === '/api/invoices/inv1/balance-exceptions' && body !== undefined) {
        return Response.json(
          {
            id: 'x1',
            invoice_id: 'inv1',
            retail_invoice_id: null,
            authorized_by: 'u2',
            reason: body.reason,
            outstanding_cents_at_authorization: current.client_outstanding_cents,
            authorized_at: new Date().toISOString(),
          },
          { status: 201 },
        )
      }
      return undefined
    },
  }
}

function stub(opts: { capabilities?: string[]; mode?: 'staff' | 'admin'; invoice?: ReturnType<typeof serviceInvoice> } = {}) {
  const { capabilities = ['billing.view'], mode = 'staff' } = opts
  const server = paymentsServer(opts.invoice ?? serviceInvoice())
  const api = stubApi({
    signedIn: true,
    respond: (url: string, body: any) => {
      if (url === '/api/auth/me') return Response.json(ACCOUNT(capabilities, mode))
      return server.respond(url, body)
    },
  })
  return { ...api, server }
}

test('recording a client payment pre-fills the outstanding balance and closes with Record another offered', async () => {
  const user = userEvent.setup()
  stub()
  renderApp('/bills/invoices/inv1')

  await screen.findByRole('heading', { name: 'Invoice #42' })
  await user.click(screen.getByRole('button', { name: 'Record payment' }))

  const dialog = await screen.findByRole('dialog')
  const amountInput = within(dialog).getByLabelText('Amount') as HTMLInputElement
  expect(amountInput.value).toBe('50.00')
  expect(within(dialog).getByRole('button', { name: 'Client', pressed: true })).toBeInTheDocument()

  // Half now, leaving a balance — Record another should be offered next.
  await user.clear(amountInput)
  await user.type(amountInput, '20.00')
  await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))

  await within(dialog).findByText('Payment recorded.')
  expect(within(dialog).getByRole('button', { name: 'Record another' })).toBeInTheDocument()
})

test('an insurer payment fixes the method, offers pending/received, and labels the reference Claim #', async () => {
  const user = userEvent.setup()
  stub()
  renderApp('/bills/invoices/inv1')

  await screen.findByRole('heading', { name: 'Invoice #42' })
  await user.click(screen.getByRole('button', { name: 'Record payment' }))
  const dialog = await screen.findByRole('dialog')

  await user.click(within(dialog).getByRole('button', { name: 'Insurer' }))
  expect(within(dialog).getByText('Method: Insurer')).toBeInTheDocument()
  expect(within(dialog).getByLabelText('Claim #')).toBeInTheDocument()

  await user.type(within(dialog).getByLabelText('Claim #'), 'CLM-9')
  await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))
  await within(dialog).findByText('Payment recorded.')

  await user.click(within(dialog).getByRole('button', { name: 'Done' }))
  expect(await screen.findByText('Pending')).toBeInTheDocument()
  expect(screen.getByText('CLM-9')).toBeInTheDocument()
})

test('mark received settles a pending insurer entry', async () => {
  const user = userEvent.setup()
  stub()
  renderApp('/bills/invoices/inv1')

  await screen.findByRole('heading', { name: 'Invoice #42' })
  await user.click(screen.getByRole('button', { name: 'Record payment' }))
  const dialog = await screen.findByRole('dialog')
  await user.click(within(dialog).getByRole('button', { name: 'Insurer' }))
  await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))
  await within(dialog).findByText('Payment recorded.')
  await user.click(within(dialog).getByRole('button', { name: 'Done' }))

  await screen.findByText('Pending')
  await user.click(screen.getByRole('button', { name: 'Mark received' }))

  // The pending entry is never edited (spec #95's own rule — a later received row settles it
  // "without editing it"); a second, received row lands alongside it and the balance settles.
  await waitFor(() => expect(screen.getAllByText('Received').length).toBeGreaterThan(0))
  expect(screen.getByText('Pending')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Mark received' })).not.toBeInTheDocument()
})

test('correcting a payment requires a reason and the original renders struck through and linked', async () => {
  const user = userEvent.setup()
  stub()
  renderApp('/bills/invoices/inv1')

  await screen.findByRole('heading', { name: 'Invoice #42' })
  await user.click(screen.getByRole('button', { name: 'Record payment' }))
  let dialog = await screen.findByRole('dialog')
  const amountInput = within(dialog).getByLabelText('Amount') as HTMLInputElement
  await user.clear(amountInput)
  await user.type(amountInput, '20.00')
  await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))
  await within(dialog).findByText('Payment recorded.')
  await user.click(within(dialog).getByRole('button', { name: 'Done' }))

  await user.click(await screen.findByRole('button', { name: 'Correct' }))
  dialog = await screen.findByRole('dialog')
  const saveButton = within(dialog).getByRole('button', { name: 'Save correction' })
  expect(saveButton).toBeDisabled()

  const correctAmount = within(dialog).getByLabelText('Amount') as HTMLInputElement
  await user.clear(correctAmount)
  await user.type(correctAmount, '25.00')
  expect(saveButton).toBeDisabled() // still no reason

  await user.type(within(dialog).getByLabelText('Reason'), 'Typo — meant $25')
  expect(saveButton).not.toBeDisabled()
  await user.click(saveButton)

  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(screen.getByText('$20.00')).toHaveClass('line-through')
  expect(screen.getByText('Superseded by correction')).toBeInTheDocument()
})

test('refund and balance exception are absent in Staff Mode', async () => {
  stub({ capabilities: ['billing.view'], mode: 'staff' })
  renderApp('/bills/invoices/inv1')

  await screen.findByRole('heading', { name: 'Invoice #42' })
  expect(screen.queryByRole('button', { name: 'Refund' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Balance exception' })).not.toBeInTheDocument()
})

test('refund and balance exception are absent without billing.manage even in Admin Mode', async () => {
  stub({ capabilities: ['billing.view'], mode: 'admin' })
  renderApp('/bills/invoices/inv1')

  await screen.findByRole('heading', { name: 'Invoice #42' })
  expect(screen.queryByRole('button', { name: 'Refund' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Balance exception' })).not.toBeInTheDocument()
})

test('refund and balance exception render for billing.manage in Admin Mode', async () => {
  const user = userEvent.setup()
  stub({ capabilities: ['billing.view', 'billing.manage'], mode: 'admin' })
  renderApp('/bills/invoices/inv1')

  await screen.findByRole('heading', { name: 'Invoice #42' })
  await user.click(screen.getByRole('button', { name: 'Refund' }))
  const dialog = await screen.findByRole('dialog')
  await user.type(within(dialog).getByLabelText('Amount'), '10.00')
  await user.type(within(dialog).getByLabelText('Reason'), 'Overcharged')
  await user.click(within(dialog).getByRole('button', { name: 'Record refund' }))

  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
})

// --- the reusable dialog on its own (Sell/package purchase's own use, #106/#108) ---------------

test('?pay=1 opens Record payment on arrival', async () => {
  stub()
  renderApp('/bills/invoices/inv1?pay=1')

  // The dialog opens modal, which marks the rest of the page `aria-hidden` — so the heading
  // behind it is unreachable by role until the dialog closes; the dialog itself is the proof.
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByRole('heading', { name: 'Record payment' })).toBeInTheDocument()
})

test('RecordPaymentDialog is a standalone, reusable export — the same shape Sell (#106) and package purchase (#108) open pre-filled', async () => {
  const user = userEvent.setup()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/retail-invoices/rs9/payments' && init?.method === 'POST') {
        const body = JSON.parse(init.body as string)
        return Response.json({ id: 'p1', ...body, retail_invoice_id: 'rs9' }, { status: 201 })
      }
      throw new Error(`unexpected fetch: ${url}`)
    }),
  )

  function Harness() {
    return (
      <QueryClientProvider client={createQueryClient()}>
        <RecordPaymentDialog
          invoiceId="rs9"
          kind="retail"
          open
          onOpenChange={() => {}}
          defaultAmountCents={4200}
          onRecorded={() => {}}
        />
      </QueryClientProvider>
    )
  }
  render(<Harness />)

  const dialog = await screen.findByRole('dialog')
  expect((within(dialog).getByLabelText('Amount') as HTMLInputElement).value).toBe('42.00')
  await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))
  await within(dialog).findByText('Payment recorded.')
})
