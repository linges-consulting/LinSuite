import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * Cancel & replace (#107, spec #95 stories 30-33): the dialog on the invoice view, and the
 * end-to-end landing on the replacement draft with its "Replaces #N" banner. Admin-Mode/
 * capability gating and "absent once cancelled" are proven by `invoice-view.test.tsx`
 * ("cancel & replace ... gated placeholders") — this file adds the one combination that file
 * doesn't cover (held capability, Staff Mode) and everything the dialog itself does.
 */

afterEach(() => vi.unstubAllGlobals())

type Account = {
  id: string
  email: string
  role: string
  capabilities: string[]
  mode: 'staff' | 'admin'
  can_switch_modes: boolean
  admin_grant_expires_at: string | null
  admin_hard_limit_at: string | null
  must_change_password: boolean
  mfa: {
    enrolled: boolean
    method: string | null
    pending: boolean
    enrolment_required: boolean
    verified_at: string | null
    email_otp_allowed: boolean
  }
}

const ADMIN: Account = {
  id: 'u1',
  email: 'admin@cedar.example',
  role: 'Administrator',
  capabilities: ['billing.view', 'billing.manage'],
  mode: 'admin',
  can_switch_modes: true,
  admin_grant_expires_at: new Date(Date.now() + 900_000).toISOString(),
  admin_hard_limit_at: new Date(Date.now() + 900_000).toISOString(),
  must_change_password: false,
  mfa: {
    enrolled: false,
    method: null,
    pending: false,
    enrolment_required: false,
    verified_at: null,
    email_otp_allowed: false,
  },
}

const STAFF_WITH_CAPABILITY: Account = { ...ADMIN, role: 'Staff', mode: 'staff', can_switch_modes: false }

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
    computed_discount_total_cents: 0,
    computed_tax_total_cents: 0,
    computed_grand_total_cents: 12000,
    tax_totals_by_component: {},
    tax_rates_by_component: {},
    tax_convention: null,
    override_applied_cents: null,
    override_reason: null,
    override_tax_convention: null,
    grand_total_cents: 12000,
    issued_at: '2026-09-20T15:00:00Z',
    issued_by: 'u1',
    replaces_invoice_id: null,
    replaced_by_invoice_id: null,
    cancelled_at: null,
    cancel_reason: null,
    lines: [],
    ...BALANCE,
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
  replaces_invoice_id: 'inv1',
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
    lines: [],
    replaces_invoice_id: null,
    replaced_by_invoice_id: null,
    cancelled_at: null,
    cancel_reason: null,
    ...BALANCE,
    outstanding_cents: 0,
    ...overrides,
  }
}

const DRAFT_SALE = {
  id: 's1',
  status: 'draft' as const,
  customer_id: null,
  sold_by_staff_id: 'st1',
  payment_collector_staff_id: null,
  replaces_retail_invoice_id: 'rinv1',
  lines: [],
  eligible_discounts: [],
  discount_ids: [],
  subtotal_cents: 0,
  discount_total_cents: 0,
  tax_totals_by_component: {},
  tax_total_cents: 0,
  grand_total_cents: 0,
  created_at: '2026-09-27T14:10:00Z',
  updated_at: '2026-09-27T14:10:00Z',
}

/** Every stub in this file goes through here: the account, whichever invoice is under test,
 *  its empty payments/refunds/balance-exceptions ledger (that panel renders on the same page),
 *  the cancel endpoint, and the fixed replacement draft (bill or sale) it lands on. */
function stub(opts: {
  account?: Account
  invoice?: ReturnType<typeof serviceInvoice>
  retail?: ReturnType<typeof retailInvoice>
  cancelResponse?: (body: any) => Response
}) {
  const account = opts.account ?? ADMIN
  const calls: { url: string; body: any }[] = []
  const api = stubApi({
    signedIn: true,
    respond: (url: string, body: any) => {
      const parsed = new URL(url, 'http://test')
      if (url === '/api/auth/me') return Response.json(account)
      if (url === '/api/catalog/products') return Response.json({ products: [] })

      if (opts.invoice) {
        const inv = opts.invoice
        if (parsed.pathname === `/api/invoices/${inv.id}` && body === undefined) {
          return Response.json(inv)
        }
        if (
          ['payments', 'refunds', 'balance-exceptions'].some(
            (kind) => parsed.pathname === `/api/invoices/${inv.id}/${kind}`,
          )
        ) {
          return Response.json({ payments: [], transfers: [], refunds: [], exceptions: [] })
        }
        if (parsed.pathname === `/api/invoices/${inv.id}/cancel`) {
          calls.push({ url, body })
          if (opts.cancelResponse) return opts.cancelResponse(body)
          return Response.json({
            invoice: { ...inv, status: 'cancelled', cancelled_at: '2026-09-28T10:00:00Z', cancel_reason: body.reason },
            replacement_bill_id: 'b1',
          })
        }
      }
      if (parsed.pathname === '/api/bills/b1') return Response.json(DRAFT_BILL)

      if (opts.retail) {
        const inv = opts.retail
        if (parsed.pathname === `/api/retail-invoices/${inv.id}` && body === undefined) {
          return Response.json(inv)
        }
        if (
          ['payments', 'refunds', 'balance-exceptions'].some(
            (kind) => parsed.pathname === `/api/retail-invoices/${inv.id}/${kind}`,
          )
        ) {
          return Response.json({ payments: [], transfers: [], refunds: [], exceptions: [] })
        }
        if (parsed.pathname === `/api/retail-invoices/${inv.id}/cancel`) {
          calls.push({ url, body })
          if (opts.cancelResponse) return opts.cancelResponse(body)
          return Response.json({
            invoice: { ...inv, status: 'cancelled', cancelled_at: '2026-09-28T10:00:00Z', cancel_reason: body.reason },
            replacement_sale_id: 's1',
          })
        }
      }
      if (parsed.pathname === '/api/retail-sales/s1') return Response.json(DRAFT_SALE)

      return undefined
    },
  })
  return { ...api, calls }
}

test('the submit button is disabled until a reason is given', async () => {
  const user = userEvent.setup()
  stub({ invoice: serviceInvoice() })
  renderApp('/bills/invoices/inv1')

  await screen.findByRole('heading', { name: 'Invoice #42' })
  await user.click(screen.getByRole('button', { name: 'Cancel' }))
  const dialog = screen.getByRole('dialog')

  expect(within(dialog).getByRole('button', { name: 'Cancel & replace' })).toBeDisabled()
  await user.type(within(dialog).getByLabelText('Reason'), 'Booked the wrong service')
  expect(within(dialog).getByRole('button', { name: 'Cancel & replace' })).toBeEnabled()
})

test('cancel is absent in Staff Mode even when the account holds billing.manage', async () => {
  stub({ invoice: serviceInvoice(), account: STAFF_WITH_CAPABILITY })
  renderApp('/bills/invoices/inv1')

  await screen.findByRole('heading', { name: 'Invoice #42' })
  expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument()
})

test('a service cancel lands on bill review with the Replaces banner, no extra fetch needed', async () => {
  const user = userEvent.setup()
  const server = stub({ invoice: serviceInvoice() })
  renderApp('/bills/invoices/inv1')

  await screen.findByRole('heading', { name: 'Invoice #42' })
  await user.click(screen.getByRole('button', { name: 'Cancel' }))
  const dialog = screen.getByRole('dialog')
  await user.type(within(dialog).getByLabelText('Reason'), 'Booked the wrong service')
  await user.click(within(dialog).getByRole('button', { name: 'Cancel & replace' }))

  await waitFor(() => expect(server.calls).toHaveLength(1))
  expect(server.calls[0].body).toEqual({ reason: 'Booked the wrong service' })

  await screen.findByRole('heading', { name: 'Priya Nair' })
  expect(screen.getByText('Replaces #42')).toBeInTheDocument()
  expect(screen.getByText('Booked the wrong service')).toBeInTheDocument()
})

test('a retail cancel lands on the Sell draft with the Replaces banner', async () => {
  const user = userEvent.setup()
  const server = stub({ retail: retailInvoice() })
  renderApp('/bills/invoices/rinv1?kind=retail')

  await screen.findByRole('heading', { name: 'Retail invoice #7' })
  await user.click(screen.getByRole('button', { name: 'Cancel' }))
  const dialog = screen.getByRole('dialog')
  await user.type(within(dialog).getByLabelText('Reason'), 'Rang up the wrong item')
  await user.click(within(dialog).getByRole('button', { name: 'Cancel & replace' }))

  await waitFor(() => expect(server.calls).toHaveLength(1))

  await screen.findByText('Draft sale')
  expect(screen.getByText('Replaces #7')).toBeInTheDocument()
  expect(screen.getByText('Rang up the wrong item')).toBeInTheDocument()
})

test('a server refusal is shown clearly and the invoice stays put', async () => {
  const user = userEvent.setup()
  stub({
    invoice: serviceInvoice(),
    cancelResponse: () => Response.json({ detail: 'No such invoice.' }, { status: 404 }),
  })
  renderApp('/bills/invoices/inv1')

  await screen.findByRole('heading', { name: 'Invoice #42' })
  await user.click(screen.getByRole('button', { name: 'Cancel' }))
  const dialog = screen.getByRole('dialog')
  await user.type(within(dialog).getByLabelText('Reason'), 'Booked the wrong service')
  await user.click(within(dialog).getByRole('button', { name: 'Cancel & replace' }))

  expect(await within(dialog).findByRole('alert')).toHaveTextContent('No such invoice.')
  // Still on the invoice, dialog open — a refusal never navigates anywhere.
  expect(within(dialog).getByText('Cancel & replace invoice #42')).toBeInTheDocument()
})
