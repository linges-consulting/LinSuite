import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * The invoice view (#102, spec #95 stories 15-41): lines with discounts and per-line tax, the
 * override adjustment when present, the full balance breakdown, replacement lineage both ways,
 * "Walk-in" for an anonymous retail invoice, and issuing a reviewed bill landing here. The
 * payments/documents/returns/cancel action slots are proven as gated placeholders only — their
 * own tickets (#103-#105, #107) grow real behaviour in their own files.
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
    computed_subtotal_cents: 12000,
    computed_discount_total_cents: 1200,
    computed_tax_total_cents: 540,
    computed_grand_total_cents: 11340,
    tax_totals_by_component: { GST: 540 },
    tax_rates_by_component: { GST: 500 },
    tax_convention: null,
    override_applied_cents: null,
    override_reason: null,
    override_tax_convention: null,
    grand_total_cents: 11340,
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
        discounted_cents: 10800,
        pretax_cents: 10800,
        tax_cents: 540,
        line_total_cents: 11340,
        prepaid_cents: 0,
        tax_convention: 'exclusive' as const,
        override_adjustment_cents: 0,
        discounts: [
          {
            discount_id: 'd1',
            discount_name: 'Autumn 10%',
            discount_kind: 'percentage' as const,
            percentage_bp: 1000,
            amount_cents: null,
            resolved_amount_cents: 1200,
          },
        ],
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
    outstanding_cents: 0,
    ...overrides,
  }
}

const DRAFT_BILL = {
  id: 'b1',
  status: 'draft' as const,
  customer: { id: 'c1', name: 'Priya Nair' },
  booking_group_id: null,
  created_at: '2026-09-27T14:00:00Z',
  updated_at: '2026-09-27T14:00:00Z',
  lines: [],
  eligible_discounts: [],
  subtotal_cents: 12000,
  discount_total_cents: 0,
  tax_totals_by_component: {},
  tax_total_cents: 0,
  grand_total_cents: 12000,
  override_total_cents: null,
  override_reason: null,
  bill_override_requests_enabled: false,
  inline_admin_bill_edit_enabled: false,
}

function stub(
  opts: {
    capabilities?: string[]
    mode?: 'staff' | 'admin'
    invoice?: ReturnType<typeof serviceInvoice>
    retail?: ReturnType<typeof retailInvoice>
    bill?: typeof DRAFT_BILL
  } = {},
) {
  const { capabilities = ['billing.view'], mode = 'staff' } = opts
  let invoice = opts.invoice
  const bill = opts.bill
  return stubApi({
    signedIn: true,
    respond: (url: string, body: any) => {
      const parsed = new URL(url, 'http://test')
      if (url === '/api/auth/me') return Response.json(ACCOUNT(capabilities, mode))
      if (bill && parsed.pathname === `/api/bills/${bill.id}`) return Response.json(bill)
      if (bill && parsed.pathname === `/api/bills/${bill.id}/issue` && body !== undefined) {
        invoice = serviceInvoice({ id: 'inv9', invoice_number: 99 })
        return Response.json(invoice, { status: 201 })
      }
      const invoiceMatch = parsed.pathname.match(/^\/api\/invoices\/([^/]+)$/)
      if (invoiceMatch && invoice && invoiceMatch[1] === invoice.id) return Response.json(invoice)
      if (opts.retail && parsed.pathname === `/api/retail-invoices/${opts.retail.id}`) {
        return Response.json(opts.retail)
      }
      return undefined
    },
  })
}

// --- rendering: lines, discounts, tax, override, balance -------------------------------------

test('a service invoice shows its lines, discount, per-line tax, the override and the balance breakdown', async () => {
  stub({
    invoice: serviceInvoice({ override_applied_cents: 10000, override_reason: 'Goodwill match' }),
  })
  renderApp('/bills/invoices/inv1')

  expect(await screen.findByRole('heading', { name: 'Invoice #42' })).toBeInTheDocument()
  expect(screen.getByText('Swedish Massage')).toBeInTheDocument()
  expect(screen.getByText('Ana Rossi')).toBeInTheDocument()
  expect(screen.getByText('Autumn 10%')).toBeInTheDocument()
  expect(screen.getByText('-$12.00')).toBeInTheDocument() // discount
  expect(screen.getByText('$5.40')).toBeInTheDocument() // tax
  expect(screen.getByText('Admin-authorized adjustment')).toBeInTheDocument()
  expect(screen.getByText('Goodwill match')).toBeInTheDocument()
  expect(screen.getByText('$100.00')).toBeInTheDocument() // override billed total
  expect(screen.getByText('Balance')).toBeInTheDocument()
  expect(screen.getByText('$50.00')).toBeInTheDocument() // outstanding_cents
})

test('a retail invoice shows Walk-in for an anonymous sale', async () => {
  stub({ retail: retailInvoice() })
  renderApp('/bills/invoices/rinv1?kind=retail')

  expect(await screen.findByRole('heading', { name: 'Retail invoice #7' })).toBeInTheDocument()
  expect(screen.getByText('Walk-in', { exact: false })).toBeInTheDocument()
  expect(screen.getByText('Shampoo 250ml')).toBeInTheDocument()
})

// --- lineage, both directions -----------------------------------------------------------------

test('lineage links render in both directions', async () => {
  stub({
    invoice: serviceInvoice({
      id: 'inv2',
      replaces_invoice_id: 'inv-old',
      replaced_by_invoice_id: 'inv-new',
    }),
  })
  renderApp('/bills/invoices/inv2')

  expect(await screen.findByRole('link', { name: /view the original/i })).toHaveAttribute(
    'href',
    '/bills/invoices/inv-old',
  )
  expect(screen.getByRole('link', { name: /view the replacement/i })).toHaveAttribute(
    'href',
    '/bills/invoices/inv-new',
  )
})

// --- issuing from bill review --------------------------------------------------------------

test('issuing an invoice from the bill review screen lands on the new invoice', async () => {
  const user = userEvent.setup()
  stub({ bill: DRAFT_BILL })
  renderApp('/bills/b1')

  await user.click(await screen.findByRole('button', { name: 'Issue invoice' }))

  expect(await screen.findByRole('heading', { name: 'Invoice #99' })).toBeInTheDocument()
})

// --- action slots: gated placeholders --------------------------------------------------------

test('payments and documents slots render for any billing.view account', async () => {
  stub({ invoice: serviceInvoice() })
  renderApp('/bills/invoices/inv1')

  await screen.findByRole('heading', { name: 'Invoice #42' })
  expect(screen.getByText('Payments')).toBeInTheDocument()
  expect(screen.getByText('Print, email & receipts')).toBeInTheDocument()
})

test('cancel & replace and returns are absent without billing.manage', async () => {
  stub({ retail: retailInvoice() })
  renderApp('/bills/invoices/rinv1?kind=retail')

  await screen.findByRole('heading', { name: 'Retail invoice #7' })
  expect(screen.queryByText('Cancel & replace')).not.toBeInTheDocument()
  expect(screen.queryByText('Returns')).not.toBeInTheDocument()
})

test('cancel & replace and returns render in Admin Mode with billing.manage', async () => {
  stub({
    retail: retailInvoice(),
    capabilities: ['billing.view', 'billing.manage'],
    mode: 'admin',
  })
  renderApp('/bills/invoices/rinv1?kind=retail')

  await screen.findByRole('heading', { name: 'Retail invoice #7' })
  expect(screen.getByText('Cancel & replace')).toBeInTheDocument()
  expect(screen.getByText('Returns')).toBeInTheDocument()
})

test('a cancelled invoice has no cancel & replace slot', async () => {
  stub({
    invoice: serviceInvoice({ status: 'cancelled', cancel_reason: 'Wrong client billed' }),
    capabilities: ['billing.view', 'billing.manage'],
    mode: 'admin',
  })
  renderApp('/bills/invoices/inv1')

  await screen.findByRole('heading', { name: 'Invoice #42' })
  expect(screen.getByText('Wrong client billed')).toBeInTheDocument()
  expect(screen.queryByText('Cancel & replace')).not.toBeInTheDocument()
})
