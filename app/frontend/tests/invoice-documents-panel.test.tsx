import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * Print, email and treatment receipts (#104, spec #95 stories 34-41): a 202 print route polled
 * every two seconds for up to thirty, a treatment receipt action gated on the invoice's own
 * checkout-complete balance, and email that confirms the client's address on file (or asks for
 * a typed one for an anonymous sale or a client with none).
 */

const ACCOUNT = (capabilities: string[] = ['billing.view']) => ({
  id: 'u2',
  email: 'desk@cedar.example',
  role: 'Staff',
  capabilities,
  mode: 'staff' as const,
  can_switch_modes: false,
  admin_grant_expires_at: null,
  admin_hard_limit_at: null,
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
  outstanding_cents: 0,
  pending_insurer_cents: 0,
  client_outstanding_cents: 0,
  checkout_complete: true,
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
    computed_subtotal_cents: 12000,
    computed_discount_total_cents: 0,
    computed_tax_total_cents: 540,
    computed_grand_total_cents: 12540,
    tax_totals_by_component: { GST: 540 },
    tax_rates_by_component: { GST: 500 },
    tax_convention: null,
    override_applied_cents: null,
    override_reason: null,
    override_tax_convention: null,
    grand_total_cents: 12540,
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
        price_cents: 12000,
        discounted_cents: 12000,
        pretax_cents: 12000,
        tax_cents: 540,
        line_total_cents: 12540,
        prepaid_cents: 0,
        tax_convention: 'exclusive' as const,
        override_adjustment_cents: 0,
        discounts: [],
        taxes: [{ component_code: 'GST', rate_bp: 500, amount_cents: 540 }],
      },
    ],
    ...BALANCE,
    ...overrides,
  }
}

function retailInvoice(overrides: Record<string, unknown> = {}) {
  return {
    id: 'rinv1',
    invoice_number: 7,
    retail_sale_id: 'rs1',
    customer_id: null,
    customer_name: null,
    status: 'issued' as const,
    subtotal_cents: 2500,
    discount_total_cents: 0,
    tax_total_cents: 125,
    tax_totals_by_component: { GST: 125 },
    grand_total_cents: 2625,
    sold_by_staff_id: 'st1',
    payment_collector_staff_id: null,
    issued_at: '2026-09-18T15:00:00Z',
    issued_by: 'u1',
    lines: [
      {
        id: 'rl1',
        variant_id: 'v1',
        variant_name: 'Shampoo 250ml',
        quantity: 1,
        unit_price_cents: 2500,
        discount_cents: 0,
        tax_cents: 125,
        tax_convention: 'exclusive' as const,
        line_total_cents: 2625,
        discounts: [],
        taxes: [{ component_code: 'GST', rate_bp: 500, amount_cents: 125 }],
        staff_id: 'st1',
      },
    ],
    replaces_invoice_id: null,
    replaced_by_invoice_id: null,
    cancelled_at: null,
    cancel_reason: null,
    ...BALANCE,
    ...overrides,
  }
}

type Handler = (url: string, body: any) => Response | undefined

/** Route matching plus the harness's own `respond` shape, extended with per-URL call counts
 *  (`pending(url, n)`) so a print route can answer 202 for its first `n` calls and 200 after. */
function stub(opts: {
  invoice?: ReturnType<typeof serviceInvoice>
  retail?: ReturnType<typeof retailInvoice>
  extra?: Handler
} = {}) {
  const calls: Record<string, number> = {}
  const pdf = (body = '%PDF-1.4 fake') =>
    new Response(body, { headers: { 'Content-Type': 'application/pdf' } })

  return stubApi({
    signedIn: true,
    respond: (url: string, body: any) => {
      const parsed = new URL(url, 'http://test')
      const path = parsed.pathname
      calls[path] = (calls[path] ?? 0) + 1

      if (path === '/api/auth/me') return Response.json(ACCOUNT())
      if (opts.invoice && path === `/api/invoices/${opts.invoice.id}`) return Response.json(opts.invoice)
      if (opts.retail && path === `/api/retail-invoices/${opts.retail.id}`) return Response.json(opts.retail)

      const extraResult = opts.extra?.(url, body)
      if (extraResult) return extraResult

      // Every print route in this file defaults to a straight 200 unless the test's own
      // `extra` handler above intercepts it (the 202-polling tests do).
      if (path.endsWith('/pdf')) return pdf()
      // The payments panel (#103) renders on the same invoice page; an empty ledger is enough.
      if (path.endsWith('/payments')) return Response.json({ payments: [], transfers: [] })
      if (path.endsWith('/refunds')) return Response.json({ refunds: [] })
      if (path.endsWith('/balance-exceptions')) return Response.json({ exceptions: [] })
      return undefined
    },
  })
}

const typing = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime })

async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:fake'), revokeObjectURL: vi.fn() }))
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function fakeTab() {
  return { location: { replace: vi.fn() }, close: vi.fn(), opener: undefined as unknown }
}

// --- print: 202 polling, success, timeout -----------------------------------------------------

test('printing an invoice that is already rendered opens it straight away', async () => {
  stub({ invoice: serviceInvoice() })
  const tab = fakeTab()
  vi.stubGlobal('open', vi.fn(() => tab))
  const user = typing()
  renderApp('/bills/invoices/inv1')

  await user.click(await screen.findByRole('button', { name: 'Print invoice' }))

  await waitFor(() => expect(tab.location.replace).toHaveBeenCalledWith('blob:fake'))
})

test('a still-rendering invoice shows "Preparing…" and opens once ready', async () => {
  let pdfCalls = 0
  stub({
    invoice: serviceInvoice(),
    extra: (url) => {
      const path = new URL(url, 'http://test').pathname
      if (path === '/api/customers/c1/invoices/inv1/pdf') {
        pdfCalls += 1
        return pdfCalls < 3
          ? Response.json({ status: 'rendering' }, { status: 202 })
          : new Response('%PDF-1.4 fake', { headers: { 'Content-Type': 'application/pdf' } })
      }
      return undefined
    },
  })
  const tab = fakeTab()
  vi.stubGlobal('open', vi.fn(() => tab))
  const user = typing()
  renderApp('/bills/invoices/inv1')

  await user.click(await screen.findByRole('button', { name: 'Print invoice' }))
  expect(await screen.findByRole('button', { name: 'Preparing…' })).toBeInTheDocument()

  await tick(2000)
  await tick(2000)

  await waitFor(() => expect(tab.location.replace).toHaveBeenCalledWith('blob:fake'))
  expect(pdfCalls).toBe(3)
})

test('a document still rendering after 30 seconds says so', async () => {
  stub({
    invoice: serviceInvoice(),
    extra: (url) => {
      const path = new URL(url, 'http://test').pathname
      if (path === '/api/customers/c1/invoices/inv1/pdf') {
        return Response.json({ status: 'rendering' }, { status: 202 })
      }
      return undefined
    },
  })
  vi.stubGlobal('open', vi.fn(() => fakeTab()))
  const user = typing()
  renderApp('/bills/invoices/inv1')

  await user.click(await screen.findByRole('button', { name: 'Print invoice' }))
  expect(await screen.findByRole('button', { name: 'Preparing…' })).toBeInTheDocument()

  await tick(30_000)

  expect(
    await screen.findByRole('button', { name: 'Still preparing — try again in a moment' }),
  ).toBeInTheDocument()
})

// --- treatment receipts: released vs. not -------------------------------------------------

test('a released line offers print and email; an unreleased one explains why not', async () => {
  stub({ invoice: serviceInvoice({ checkout_complete: true }) })
  vi.stubGlobal('open', vi.fn(() => fakeTab()))
  renderApp('/bills/invoices/inv1')

  // "Swedish Massage" also names the line in the invoice's own lines table (#102) — the
  // receipt row's own `role="group"` name is the unambiguous way to scope this query.
  const row = within(await screen.findByRole('group', { name: 'Swedish Massage' }))
  expect(row.getByRole('button', { name: 'Print' })).toBeInTheDocument()
  expect(row.getByRole('button', { name: 'Email' })).toBeInTheDocument()
})

test('an unreleased line shows the explanation, never a button that would 409', async () => {
  stub({ invoice: serviceInvoice({ checkout_complete: false, status: 'issued' }) })
  renderApp('/bills/invoices/inv1')

  const row = within(await screen.findByRole('group', { name: 'Swedish Massage' }))
  expect(row.getByText('Receipt available after checkout')).toBeInTheDocument()
  expect(row.queryByRole('button')).not.toBeInTheDocument()
})

// --- email: address on file, typed fallback, toast --------------------------------------------

test('emailing a service invoice confirms and uses the address on file', async () => {
  const stubbed = stub({
    invoice: serviceInvoice(),
    extra: (url) => {
      const path = new URL(url, 'http://test').pathname
      if (path === '/api/customers/c1/invoices/inv1/email') {
        return Response.json({ status: 'queued', to: 'priya@example.com' })
      }
      return undefined
    },
  })
  const user = typing()
  renderApp('/bills/invoices/inv1')

  await user.click(await screen.findByRole('button', { name: 'Email invoice' }))
  expect(
    await screen.findByText("This will be sent to the client's email address on file."),
  ).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Send' }))

  expect(await screen.findByText('Email queued to priya@example.com')).toBeInTheDocument()
  const call = stubbed.calls.find((c) => c.url.endsWith('/invoices/inv1/email'))
  expect(call?.body).toEqual({})
})

test('a client with no email on file is asked for one instead of a dead end', async () => {
  let attempts = 0
  const stubbed = stub({
    invoice: serviceInvoice(),
    extra: (url, body) => {
      const path = new URL(url, 'http://test').pathname
      if (path === '/api/customers/c1/invoices/inv1/email') {
        attempts += 1
        if (!body?.to) {
          return Response.json(
            { detail: 'This client has no email address on file.' },
            { status: 422 },
          )
        }
        return Response.json({ status: 'queued', to: body.to })
      }
      return undefined
    },
  })
  const user = typing()
  renderApp('/bills/invoices/inv1')

  await user.click(await screen.findByRole('button', { name: 'Email invoice' }))
  await user.click(await screen.findByRole('button', { name: 'Send' }))

  expect(
    await screen.findByText('This client has no email address on file. Enter one to send to.'),
  ).toBeInTheDocument()
  await user.type(screen.getByLabelText('Email address'), 'typed@example.com')
  await user.click(screen.getByRole('button', { name: 'Send' }))

  expect(await screen.findByText('Email queued to typed@example.com')).toBeInTheDocument()
  expect(attempts).toBe(2)
  const calls = stubbed.calls.filter((c) => c.url.endsWith('/invoices/inv1/email'))
  expect(calls[1].body).toEqual({ to: 'typed@example.com' })
})

test('an anonymous retail sale asks for an address straight away', async () => {
  stub({
    retail: retailInvoice(),
    extra: (url, body) => {
      const path = new URL(url, 'http://test').pathname
      if (path === '/api/retail-invoices/rinv1/email') {
        return body?.to
          ? Response.json({ status: 'queued', to: body.to })
          : Response.json({ detail: 'Enter a recipient email for an anonymous sale.' }, { status: 422 })
      }
      return undefined
    },
  })
  const user = typing()
  renderApp('/bills/invoices/rinv1?kind=retail')

  await user.click(await screen.findByRole('button', { name: 'Email retail invoice' }))

  // Straight into the typed field — never the "on file" confirmation, since there is no client.
  expect(screen.queryByText(/email address on file/)).not.toBeInTheDocument()
  await user.type(screen.getByLabelText('Email address'), 'walkin@example.com')
  await user.click(screen.getByRole('button', { name: 'Send' }))

  expect(await screen.findByText('Email queued to walkin@example.com')).toBeInTheDocument()
})
