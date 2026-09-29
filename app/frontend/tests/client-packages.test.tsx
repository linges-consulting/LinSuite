import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

afterEach(() => vi.unstubAllGlobals())

/**
 * A client's Packages tab (#108, spec #95 user stories 51-54): purchases with remaining
 * credits per service, expiry and status, plus *Sell package* landing on the new invoice with
 * the payment dialog open. `?pay=1` itself is `invoice-payments-panel.test.tsx`'s own test —
 * this file only pins that the Sell flow reaches it.
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

const PURCHASE = {
  id: 'pp1',
  package_definition_id: 'pd1',
  name: '10-Session Massage Pack',
  price_cents: 96000,
  customer_id: 'c1',
  purchased_at: '2026-09-01T15:00:00Z',
  expires_at: null as string | null,
  credits_activated: true,
  credits_voided_at: null as string | null,
  invoice_id: 'inv-pkg1',
  invoice_number: 12,
  credits: [
    {
      service_id: 'sv1',
      service_name: 'Massage',
      credits_total: 10,
      credits_used: 3,
      credits_remaining: 7,
    },
  ],
}

const SELLABLE = {
  id: 'pd1',
  name: '10-Session Massage Pack',
  price_cents: 96000,
  expires_after_days: null as number | null,
  tax_convention: 'exclusive' as const,
  services: [{ service_id: 'sv1', service_name: 'Massage', credits: 10 }],
}

function packageInvoice() {
  return {
    id: 'inv-pkg1',
    invoice_number: 12,
    service_bill_id: null,
    package_purchase_id: 'pp1',
    customer_id: 'c1',
    customer_name: 'Priya Nair',
    status: 'issued' as const,
    computed_subtotal_cents: 96000,
    computed_discount_total_cents: 0,
    computed_tax_total_cents: 0,
    computed_grand_total_cents: 96000,
    tax_totals_by_component: {},
    tax_rates_by_component: {},
    tax_convention: 'exclusive' as const,
    override_applied_cents: null,
    override_reason: null,
    override_tax_convention: null,
    grand_total_cents: 96000,
    issued_at: '2026-09-29T15:00:00Z',
    issued_by: 'u1',
    replaces_invoice_id: null,
    replaced_by_invoice_id: null,
    cancelled_at: null,
    cancel_reason: null,
    lines: [] as unknown[],
    outstanding_cents: 96000,
    pending_insurer_cents: 0,
    client_outstanding_cents: 96000,
    checkout_complete: false,
    refunded_cents: 0,
    prepaid_cents: 0,
    held_credit_cents: 0,
  }
}

function fake({
  purchases = [PURCHASE],
  sellable = [SELLABLE],
}: {
  purchases?: (typeof PURCHASE)[]
  sellable?: (typeof SELLABLE)[]
} = {}) {
  return stubApi({
    signedIn: true,
    respond: (url, body) => {
      if (url === '/api/auth/me') return Response.json(ACCOUNT)
      if (url === '/api/customers/c1') return Response.json(PROFILE)
      if (url === '/api/customers/c1/package-purchases') return Response.json({ purchases })
      if (url === '/api/packages') return Response.json({ packages: sellable })
      if (url === '/api/packages/pd1/purchase' && body !== undefined) {
        return Response.json(
          {
            id: 'pp2',
            package_definition_id: 'pd1',
            customer_id: 'c1',
            name: '10-Session Massage Pack',
            price_cents: 96000,
            expires_after_days: null,
            expires_at: null,
            purchased_at: new Date().toISOString(),
            credits_activated: false,
            activated_at: null,
            credits_voided_at: null,
            credits: [{ service_id: 'sv1', credits_total: 10, allocated_price_cents: 96000 }],
            invoice_id: 'inv-pkg1',
            invoice_number: 12,
            computed_subtotal_cents: 96000,
            computed_tax_total_cents: 0,
            tax_totals_by_component: {},
            grand_total_cents: 96000,
          },
          { status: 201 },
        )
      }
      if (url === '/api/invoices/inv-pkg1') return Response.json(packageInvoice())
      if (url === '/api/invoices/inv-pkg1/payments') {
        return Response.json({ payments: [], transfers: [] })
      }
      if (url === '/api/invoices/inv-pkg1/refunds') return Response.json({ refunds: [] })
      if (url === '/api/invoices/inv-pkg1/balance-exceptions') {
        return Response.json({ exceptions: [] })
      }
      return undefined
    },
  })
}

test('purchases list shows remaining credits per service, expiry and status', async () => {
  fake()
  renderApp('/clients/c1')

  const table = await screen.findByRole('table', { name: 'Packages' })
  expect(within(table).getByText('10-Session Massage Pack')).toBeInTheDocument()
  expect(within(table).getByText('Massage: 7 of 10 left')).toBeInTheDocument()
  expect(within(table).getByText('Never')).toBeInTheDocument()
  expect(within(table).getByText('Active')).toBeInTheDocument()
})

test('an unpaid purchase shows as Unpaid', async () => {
  fake({ purchases: [{ ...PURCHASE, credits_activated: false }] })
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  expect(screen.getByText('Unpaid')).toBeInTheDocument()
})

test('a refunded purchase shows as Refunded, even though its credits were once activated', async () => {
  fake({ purchases: [{ ...PURCHASE, credits_voided_at: '2026-09-15T10:00:00Z' }] })
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  expect(screen.getByText('Refunded')).toBeInTheDocument()
})

test('a fully redeemed purchase shows as Fully used', async () => {
  fake({
    purchases: [
      {
        ...PURCHASE,
        credits: [{ ...PURCHASE.credits[0], credits_used: 10, credits_remaining: 0 }],
      },
    ],
  })
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  expect(screen.getByText('Fully used')).toBeInTheDocument()
})

test('with no purchases, the empty state shows', async () => {
  fake({ purchases: [] })
  renderApp('/clients/c1')

  await screen.findByRole('heading', { name: 'Priya Nair' })
  expect(await screen.findByText('No packages yet')).toBeInTheDocument()
})

test('the package name links to its invoice', async () => {
  fake()
  renderApp('/clients/c1')

  const table = await screen.findByRole('table', { name: 'Packages' })
  expect(within(table).getByRole('link', { name: '10-Session Massage Pack' })).toHaveAttribute(
    'href',
    '/bills/invoices/inv-pkg1',
  )
})

test('selling a package purchases it and lands on its invoice with the payment dialog open', async () => {
  const user = userEvent.setup()
  fake()
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  await user.click(screen.getByRole('button', { name: 'Sell package' }))

  const sellDialog = await screen.findByRole('dialog')
  within(sellDialog).getByRole('heading', { name: 'Sell a package' })
  await user.click(within(sellDialog).getByRole('combobox', { name: 'Package' }))
  await user.click(await screen.findByRole('option', { name: /10-Session Massage Pack/ }))

  expect(within(sellDialog).getByText('10 × Massage')).toBeInTheDocument()
  await user.click(within(sellDialog).getByRole('button', { name: 'Sell package' }))

  // The Sell dialog closes and the new invoice's own `?pay=1` payment dialog takes its place —
  // modal, which marks the invoice page behind it `aria-hidden` (`invoice-payments-panel
  // .test.tsx`'s own note): the payment dialog itself is the proof of arrival, not the
  // now-inaccessible heading behind it.
  await waitFor(() =>
    expect(screen.queryByRole('heading', { name: 'Sell a package' })).not.toBeInTheDocument(),
  )
  const paymentDialog = await screen.findByRole('dialog')
  expect(within(paymentDialog).getByRole('heading', { name: 'Record payment' })).toBeInTheDocument()
})

test('with no active packages, the Sell dialog says so', async () => {
  const user = userEvent.setup()
  fake({ sellable: [] })
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  await user.click(screen.getByRole('button', { name: 'Sell package' }))

  expect(await screen.findByText('No active packages to sell.')).toBeInTheDocument()
})
