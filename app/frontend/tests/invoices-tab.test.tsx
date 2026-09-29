import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi, type Call } from './harness'

afterEach(() => vi.unstubAllGlobals())

/**
 * Billing → Invoices (#99, spec #95 stories 6-14): the Service | Retail switch, date/status/
 * client filters, and the paginated table. What is worth pinning: each row's number, date,
 * client (or Walk-in), total, balance and status badge; a retail row links with `?kind=retail`
 * so #102's invoice view knows which table to read the id against; the status/client filters
 * and pagination all narrow through query params, never a client-side filter over what
 * happens to have loaded; and the date inputs show the server's own effective range.
 */

const ACCOUNT = {
  id: 'u2',
  email: 'desk@cedar.example',
  role: 'Staff',
  capabilities: ['billing.view'],
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
}

const BALANCE = {
  pending_insurer_cents: 0,
  client_outstanding_cents: 0,
  checkout_complete: true,
  refunded_cents: 0,
  prepaid_cents: 0,
  held_credit_cents: 0,
}

const OUTSTANDING_INVOICE = {
  id: 'inv1',
  invoice_number: 12,
  customer_id: 'c1',
  customer_name: 'Priya Nair',
  status: 'issued' as const,
  list_status: 'outstanding' as const,
  grand_total_cents: 12000,
  issued_at: '2026-09-20T15:00:00Z',
  ...BALANCE,
  outstanding_cents: 12000,
  client_outstanding_cents: 12000,
  checkout_complete: false,
}

const WALK_IN_RETAIL_INVOICE = {
  id: 'rinv1',
  invoice_number: 4,
  customer_id: null,
  customer_name: null,
  status: 'issued' as const,
  list_status: 'paid' as const,
  grand_total_cents: 2499,
  issued_at: '2026-09-18T15:00:00Z',
  ...BALANCE,
  outstanding_cents: 0,
}

const DEFAULT_RANGE = { from: '2026-08-30', to: '2026-09-29', timezone: 'America/Toronto' }

function fake({
  invoices = [],
  retailInvoices = [],
}: {
  invoices?: (typeof OUTSTANDING_INVOICE)[]
  retailInvoices?: (typeof WALK_IN_RETAIL_INVOICE)[]
} = {}) {
  return stubApi({
    signedIn: true,
    respond: (url) => {
      const parsed = new URL(url, 'http://test')
      if (url === '/api/auth/me') return Response.json(ACCOUNT)
      if (url === '/api/bills') return Response.json({ bills: [] })
      if (parsed.pathname === '/api/invoices') {
        const status = parsed.searchParams.get('status')
        const customerId = parsed.searchParams.get('customer_id')
        const rows = invoices.filter(
          (i) =>
            (!status || i.list_status === status) && (!customerId || i.customer_id === customerId),
        )
        return Response.json({ invoices: rows, total: rows.length, ...DEFAULT_RANGE })
      }
      if (parsed.pathname === '/api/retail-invoices') {
        return Response.json({
          retail_invoices: retailInvoices,
          total: retailInvoices.length,
          ...DEFAULT_RANGE,
        })
      }
      if (parsed.pathname === '/api/customers' && parsed.searchParams.get('q') === 'nai') {
        return Response.json({
          customers: [
            {
              id: 'c1',
              first_name: 'Priya',
              last_name: 'Nair',
              email: null,
              phone: null,
              classification: 'new',
            },
          ],
        })
      }
      return undefined
    },
  })
}

const invoiceCalls = (calls: Call[]) => calls.filter((c) => c.url.startsWith('/api/invoices?'))

test('a service row shows number, date, client, total, balance and status, and links plain', async () => {
  fake({ invoices: [OUTSTANDING_INVOICE] })
  const user = userEvent.setup()
  renderApp('/bills')

  await user.click(await screen.findByRole('tab', { name: 'Invoices' }))

  const table = await screen.findByRole('table', { name: 'Service invoices' })
  expect(within(table).getByText('#12')).toBeInTheDocument()
  expect(within(table).getByText('Priya Nair')).toBeInTheDocument()
  expect(within(table).getAllByText('$120.00')).toHaveLength(2) // total and balance both owed
  expect(within(table).getByText('Outstanding')).toBeInTheDocument()
  expect(within(table).getByRole('link', { name: '#12' })).toHaveAttribute(
    'href',
    '/bills/invoices/inv1',
  )
})

test('switching to Retail shows the retail list, Walk-in for an anonymous sale, and links with ?kind=retail', async () => {
  fake({ retailInvoices: [WALK_IN_RETAIL_INVOICE] })
  const user = userEvent.setup()
  renderApp('/bills')
  await user.click(await screen.findByRole('tab', { name: 'Invoices' }))

  await user.click(screen.getByRole('button', { name: 'Retail' }))

  const table = await screen.findByRole('table', { name: 'Retail invoices' })
  expect(within(table).getByText('#4')).toBeInTheDocument()
  expect(within(table).getByText('Walk-in')).toBeInTheDocument()
  expect(within(table).getByText('Paid')).toBeInTheDocument()
  expect(within(table).getByRole('link', { name: '#4' })).toHaveAttribute(
    'href',
    '/bills/invoices/rinv1?kind=retail',
  )
})

test('the status filter narrows the list through the status query param', async () => {
  const { calls } = fake({ invoices: [OUTSTANDING_INVOICE] })
  const user = userEvent.setup()
  renderApp('/bills')
  await user.click(await screen.findByRole('tab', { name: 'Invoices' }))
  await screen.findByText('#12')

  await user.click(screen.getByRole('combobox', { name: 'Status' }))
  await user.click(await screen.findByRole('option', { name: 'Paid' }))

  expect(await screen.findByText('No invoices in this range')).toBeInTheDocument()
  const last = invoiceCalls(calls).at(-1)!
  expect(new URL(last.url, 'http://test').searchParams.get('status')).toBe('paid')
})

test('picking a client narrows through customer_id and shows a clearable chip', async () => {
  const { calls } = fake({ invoices: [OUTSTANDING_INVOICE] })
  const user = userEvent.setup()
  renderApp('/bills')
  await user.click(await screen.findByRole('tab', { name: 'Invoices' }))
  await screen.findByText('#12')

  await user.type(screen.getByPlaceholderText('Name, phone or email'), 'nai')
  await user.click(await screen.findByRole('button', { name: 'Priya Nair' }))

  const last = invoiceCalls(calls).at(-1)!
  expect(new URL(last.url, 'http://test').searchParams.get('customer_id')).toBe('c1')
  // The chip, not the row it sits beside — both now say "Priya Nair".
  const clear = screen.getByRole('button', { name: 'Clear' })
  expect(clear.parentElement).toHaveTextContent('Priya Nair')

  await user.click(clear)
  expect(screen.queryByRole('button', { name: 'Clear' })).not.toBeInTheDocument()
})

test('an empty range is the standard empty state, and the date inputs show the server default', async () => {
  fake()
  const user = userEvent.setup()
  renderApp('/bills')

  await user.click(await screen.findByRole('tab', { name: 'Invoices' }))

  expect(await screen.findByText('No invoices in this range')).toBeInTheDocument()
  expect(screen.getByLabelText('From')).toHaveValue(DEFAULT_RANGE.from)
  expect(screen.getByLabelText('To')).toHaveValue(DEFAULT_RANGE.to)
})
