import { screen, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

afterEach(() => vi.unstubAllGlobals())

/**
 * A client's Invoices card (#99, spec #95 user story 65): both service and retail invoices,
 * merged into one newest-first list — the client's whole billing history in one place. Rows
 * come from `routes/invoices.tsx`'s own `InvoiceRow`, so this file only pins the merge/sort
 * and the empty state; row content itself is `tests/invoices-tab.test.tsx`'s job.
 */

const ACCOUNT = {
  id: 'u1',
  email: 'desk@cedar.example',
  role: 'Staff',
  capabilities: ['customers.view', 'billing.view'],
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

const PROFILE = {
  customer: {
    id: 'c1',
    first_name: 'Priya',
    last_name: 'Nair',
    email: 'priya@example.com',
    phone: '4165550199',
    created_at: '2026-01-05T15:00:00Z',
    classification: 'vip' as const,
    date_of_birth: null,
    emergency_contact_name: null,
    emergency_contact_phone: null,
    emergency_contact_relationship: null,
    secondary_contact_name: null,
    secondary_contact_phone: null,
    secondary_contact_email: null,
    notes: null,
    updated_at: '2026-01-05T15:00:00Z',
    retention: { status: 'not_held' as const, expires_on: null as string | null },
  },
  timezone: 'America/Toronto',
  notification_failures: [],
  appointments: [],
}

const BALANCE = {
  pending_insurer_cents: 0,
  client_outstanding_cents: 0,
  checkout_complete: true,
  refunded_cents: 0,
  prepaid_cents: 0,
  held_credit_cents: 0,
}

const SERVICE_INVOICE = {
  id: 'inv1',
  invoice_number: 3,
  customer_id: 'c1',
  customer_name: 'Priya Nair',
  status: 'issued' as const,
  list_status: 'paid' as const,
  grand_total_cents: 12000,
  issued_at: '2026-09-10T15:00:00Z', // older
  ...BALANCE,
  outstanding_cents: 0,
}

const RETAIL_INVOICE = {
  id: 'rinv1',
  invoice_number: 7,
  customer_id: 'c1',
  customer_name: 'Priya Nair',
  status: 'issued' as const,
  list_status: 'outstanding' as const,
  grand_total_cents: 2499,
  issued_at: '2026-09-20T15:00:00Z', // newer
  ...BALANCE,
  outstanding_cents: 2499,
}

function fake({
  invoices = [],
  retailInvoices = [],
}: {
  invoices?: (typeof SERVICE_INVOICE)[]
  retailInvoices?: (typeof RETAIL_INVOICE)[]
} = {}) {
  return stubApi({
    signedIn: true,
    respond: (url) => {
      const parsed = new URL(url, 'http://test')
      if (url === '/api/auth/me') return Response.json(ACCOUNT)
      if (parsed.pathname === '/api/customers/c1') return Response.json(PROFILE)
      if (parsed.pathname === '/api/invoices') {
        return Response.json({
          invoices,
          total: invoices.length,
          from: '2026-08-30',
          to: '2026-09-29',
          timezone: 'America/Toronto',
        })
      }
      if (parsed.pathname === '/api/retail-invoices') {
        return Response.json({
          retail_invoices: retailInvoices,
          total: retailInvoices.length,
          from: '2026-08-30',
          to: '2026-09-29',
          timezone: 'America/Toronto',
        })
      }
      return undefined
    },
  })
}

test('service and retail invoices are merged into one list, newest first', async () => {
  fake({ invoices: [SERVICE_INVOICE], retailInvoices: [RETAIL_INVOICE] })

  renderApp('/clients/c1')

  const table = await screen.findByRole('table', { name: 'Invoices' })
  const numbers = within(table)
    .getAllByRole('row')
    .slice(1) // drop the header row
    .map((row) => within(row).getAllByRole('cell')[0].textContent)
  // The retail invoice (Sep 20) is newer than the service one (Sep 10).
  expect(numbers).toEqual(['#7', '#3'])
})

test('with neither kind issued, the standard empty state shows', async () => {
  fake()

  renderApp('/clients/c1')

  await screen.findByRole('heading', { name: 'Priya Nair' })
  expect(await screen.findByText('No invoices yet')).toBeInTheDocument()
})
