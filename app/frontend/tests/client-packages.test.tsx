import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

afterEach(() => vi.unstubAllGlobals())

/**
 * A client's Packages tab (#108, spec #95 user stories 51-54; #109 stories 55-56): purchases
 * with remaining credits per service, expiry and status, *Sell package* landing on the new
 * invoice with the payment dialog open, and *Refund* — gated to Admin Mode + `billing.manage`,
 * the server's standard values pre-selected, and the manual exception forced once a credit has
 * been redeemed. `?pay=1` itself is `invoice-payments-panel.test.tsx`'s own test — this file
 * only pins that the Sell flow reaches it.
 */

const ACCOUNT = (capabilities: string[], mode: 'staff' | 'admin' = 'staff') => ({
  id: 'u1',
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
  purchaser_name: 'Priya Nair',
  current_holder_id: 'c1',
  current_holder_name: 'Priya Nair',
  held_by_viewer: true,
  transferable: false,
  transfers: [] as {
    id: string
    from_customer_id: string
    from_customer_name: string
    to_customer_id: string
    to_customer_name: string
    reason: string
    override: boolean
    transferred_at: string
  }[],
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
      used_by_you: 3,
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

function packageInvoice(overrides: Record<string, unknown> = {}) {
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
    ...overrides,
  }
}

function fake({
  purchases = [PURCHASE],
  sellable = [SELLABLE],
  capabilities = ['customers.view', 'billing.view'],
  mode = 'staff' as 'staff' | 'admin',
  invoice = packageInvoice(),
  upcoming = [] as { id: string; starts_at: string; service_name: string; staff_name: string }[],
  onRefund,
  onTransfer,
}: {
  purchases?: (typeof PURCHASE)[]
  sellable?: (typeof SELLABLE)[]
  capabilities?: string[]
  mode?: 'staff' | 'admin'
  invoice?: ReturnType<typeof packageInvoice>
  upcoming?: { id: string; starts_at: string; service_name: string; staff_name: string }[]
  onRefund?: (body: Record<string, unknown>) => Response | undefined
  onTransfer?: (body: Record<string, unknown>) => Response | undefined
} = {}) {
  return stubApi({
    signedIn: true,
    respond: (url, body) => {
      if (url === '/api/auth/me') return Response.json(ACCOUNT(capabilities, mode))
      if (url === '/api/customers/c1') return Response.json(PROFILE)
      if (url === '/api/customers/c1/package-purchases') return Response.json({ purchases })
      if (url === '/api/packages') return Response.json({ packages: sellable })
      if (url.startsWith('/api/customers?')) {
        const q = new URL(url, 'http://test').searchParams.get('q') ?? ''
        const customers = q.toLowerCase().includes('sam')
          ? [{ id: 'c2', first_name: 'Sam', last_name: 'Lee', email: null, phone: null, classification: 'new' }]
          : []
        return Response.json({ customers })
      }
      if (url === '/api/packages/purchases/pp1/upcoming-appointments') {
        return Response.json({ appointments: upcoming })
      }
      if (url === '/api/packages/purchases/pp1/transfer' && body !== undefined) {
        return (
          onTransfer?.(body) ??
          Response.json(
            {
              package_purchase_id: 'pp1',
              current_holder_id: (body as { to_customer_id: string }).to_customer_id,
              current_holder_name: 'Sam Lee',
              transfers: [
                {
                  id: 'pt1',
                  from_customer_id: 'c1',
                  from_customer_name: 'Priya Nair',
                  to_customer_id: (body as { to_customer_id: string }).to_customer_id,
                  to_customer_name: 'Sam Lee',
                  reason: (body as { reason: string }).reason,
                  override: (body as { override: boolean }).override,
                  transferred_at: new Date().toISOString(),
                },
              ],
            },
            { status: 201 },
          )
        )
      }
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
      if (url === '/api/packages/purchases/pp1/refund' && body !== undefined) {
        return (
          onRefund?.(body) ??
          Response.json(
            {
              package_purchase_id: 'pp1',
              invoice: { ...invoice, status: 'cancelled' },
              refund: {
                id: 'rf1',
                invoice_id: invoice.id,
                retail_invoice_id: null,
                amount_cents: (body.exception as { amount_cents: number } | undefined)?.amount_cents ?? 0,
                reason: body.reason,
                approved_by: 'u1',
                refunded_at: new Date().toISOString(),
              },
              credits_voided: true,
              commission_reversals: 0,
            },
            { status: 201 },
          )
        )
      }
      if (url === `/api/invoices/${invoice.id}` && body === undefined) return Response.json(invoice)
      if (url === `/api/invoices/${invoice.id}/payments`) {
        return Response.json({ payments: [], transfers: [] })
      }
      if (url === `/api/invoices/${invoice.id}/refunds`) return Response.json({ refunds: [] })
      if (url === `/api/invoices/${invoice.id}/balance-exceptions`) {
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

// --- Refund (#109, spec #95 stories 55-56) ----------------------------------------------------

test('Refund is absent in Staff Mode', async () => {
  fake({ capabilities: ['customers.view', 'billing.view', 'billing.manage'], mode: 'staff' })
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  expect(screen.queryByRole('button', { name: 'Refund' })).not.toBeInTheDocument()
})

test('Refund is absent without billing.manage, even in Admin Mode', async () => {
  fake({ capabilities: ['customers.view', 'billing.view'], mode: 'admin' })
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  expect(screen.queryByRole('button', { name: 'Refund' })).not.toBeInTheDocument()
})

test('Refund pre-selects the standard amount, says who it refunds, and sends it without a manual exception', async () => {
  const user = userEvent.setup()
  let sentBody: Record<string, unknown> | undefined
  const unredeemed = {
    ...PURCHASE,
    credits: [{ ...PURCHASE.credits[0], credits_used: 0, credits_remaining: 10 }],
  }
  fake({
    purchases: [unredeemed],
    capabilities: ['customers.view', 'billing.view', 'billing.manage'],
    mode: 'admin',
    invoice: packageInvoice({
      outstanding_cents: 0,
      client_outstanding_cents: 0,
      checkout_complete: true,
    }),
    onRefund: (body) => {
      sentBody = body
      return undefined
    },
  })
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  await user.click(screen.getByRole('button', { name: 'Refund' }))

  const dialog = await screen.findByRole('dialog')
  within(dialog).getByRole('heading', { name: 'Refund 10-Session Massage Pack' })
  expect(within(dialog).getByText('Refunds Priya Nair, who paid.')).toBeInTheDocument()
  // The server's standard amount — money received (the invoice is fully paid: outstanding is
  // 0) less prior refunds (none) — pre-selected without an extra click.
  await screen.findByText('$960.00')

  await user.type(within(dialog).getByLabelText('Reason'), 'Client moved away')
  await user.click(within(dialog).getByRole('button', { name: 'Refund' }))

  await waitFor(() => expect(sentBody).toBeDefined())
  expect(sentBody).toEqual({ reason: 'Client moved away' })
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
})

test('a redeemed credit forces the manual exception, and sends the chosen remaining-credit and commission choices', async () => {
  const user = userEvent.setup()
  let sentBody: Record<string, unknown> | undefined
  fake({
    capabilities: ['customers.view', 'billing.view', 'billing.manage'],
    mode: 'admin',
    onRefund: (body) => {
      sentBody = body
      return undefined
    },
  })
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  await user.click(screen.getByRole('button', { name: 'Refund' }))

  const dialog = await screen.findByRole('dialog')
  expect(
    within(dialog).getByText(/no longer refundable under the standard policy/),
  ).toBeInTheDocument()
  expect(within(dialog).queryByRole('checkbox', { name: 'Manual exception' })).not.toBeInTheDocument()

  await user.type(within(dialog).getByLabelText('Amount'), '200.00')
  await user.click(within(dialog).getByRole('combobox', { name: 'Remaining credits' }))
  await user.click(await screen.findByRole('option', { name: 'Cancel remaining credits' }))
  await user.click(within(dialog).getByRole('checkbox', { name: 'Reverse commission already earned' }))
  await user.type(within(dialog).getByLabelText('Reason'), 'Goodwill after a dispute')
  await user.click(within(dialog).getByRole('button', { name: 'Refund' }))

  await waitFor(() => expect(sentBody).toBeDefined())
  expect(sentBody).toEqual({
    reason: 'Goodwill after a dispute',
    exception: {
      amount_cents: 20000,
      cancel_remaining_credits: true,
      reverse_commission: true,
    },
  })
})

// --- Transfer (#111, spec #96) -----------------------------------------------------------------

test('Transfer is absent in Staff Mode', async () => {
  fake({ capabilities: ['customers.view', 'billing.view', 'billing.manage'], mode: 'staff' })
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  expect(screen.queryByRole('button', { name: 'Transfer' })).not.toBeInTheDocument()
})

test('Transfer is absent without billing.manage, even in Admin Mode', async () => {
  fake({ capabilities: ['customers.view', 'billing.view'], mode: 'admin' })
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  expect(screen.queryByRole('button', { name: 'Transfer' })).not.toBeInTheDocument()
})

test('Transfer is absent on a purchase this client no longer holds', async () => {
  fake({
    purchases: [
      {
        ...PURCHASE,
        held_by_viewer: false,
        current_holder_id: 'c2',
        current_holder_name: 'Sam Lee',
        transfers: [
          {
            id: 'pt1',
            from_customer_id: 'c1',
            from_customer_name: 'Priya Nair',
            to_customer_id: 'c2',
            to_customer_name: 'Sam Lee',
            reason: 'Client moved',
            override: false,
            transferred_at: '2026-09-20T10:00:00Z',
          },
        ],
      },
    ],
    capabilities: ['customers.view', 'billing.view', 'billing.manage'],
    mode: 'admin',
  })
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  expect(screen.queryByRole('button', { name: 'Transfer' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Refund' })).not.toBeInTheDocument()
})

test('the override checkbox appears only for a non-transferable package', async () => {
  const user = userEvent.setup()
  fake({
    purchases: [{ ...PURCHASE, transferable: false }],
    capabilities: ['customers.view', 'billing.view', 'billing.manage'],
    mode: 'admin',
  })
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  await user.click(screen.getByRole('button', { name: 'Transfer' }))
  const dialog = await screen.findByRole('dialog')

  expect(
    within(dialog).getByRole('checkbox', { name: /non-transferable by default/ }),
  ).toBeInTheDocument()
})

test('no override checkbox for a transferable package', async () => {
  const user = userEvent.setup()
  fake({
    purchases: [{ ...PURCHASE, transferable: true }],
    capabilities: ['customers.view', 'billing.view', 'billing.manage'],
    mode: 'admin',
  })
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  await user.click(screen.getByRole('button', { name: 'Transfer' }))
  const dialog = await screen.findByRole('dialog')

  expect(
    within(dialog).queryByRole('checkbox', { name: /non-transferable by default/ }),
  ).not.toBeInTheDocument()
})

test('the reason is required before Transfer can submit', async () => {
  const user = userEvent.setup()
  let sentBody: Record<string, unknown> | undefined
  fake({
    purchases: [{ ...PURCHASE, transferable: true }],
    capabilities: ['customers.view', 'billing.view', 'billing.manage'],
    mode: 'admin',
    onTransfer: (body) => {
      sentBody = body
      return undefined
    },
  })
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  await user.click(screen.getByRole('button', { name: 'Transfer' }))
  const dialog = await screen.findByRole('dialog')

  await user.type(within(dialog).getByLabelText('Client'), 'Sam')
  await user.click(await screen.findByRole('button', { name: 'Sam Lee' }))

  expect(within(dialog).getByRole('button', { name: 'Transfer' })).toBeDisabled()

  await user.type(within(dialog).getByLabelText('Reason'), 'Client moved away')
  expect(within(dialog).getByRole('button', { name: 'Transfer' })).toBeEnabled()
  await user.click(within(dialog).getByRole('button', { name: 'Transfer' }))

  await waitFor(() => expect(sentBody).toBeDefined())
  expect(sentBody).toEqual({
    to_customer_id: 'c2',
    from_customer_id: 'c1',
    reason: 'Client moved away',
    override: false,
  })
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
})

test('the upcoming-appointments warning renders', async () => {
  const user = userEvent.setup()
  fake({
    purchases: [{ ...PURCHASE, transferable: true }],
    capabilities: ['customers.view', 'billing.view', 'billing.manage'],
    mode: 'admin',
    upcoming: [
      { id: 'a1', starts_at: '2026-10-05T15:00:00Z', service_name: 'Massage', staff_name: 'Jo' },
    ],
  })
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  await user.click(screen.getByRole('button', { name: 'Transfer' }))
  const dialog = await screen.findByRole('dialog')

  expect(await within(dialog).findByText(/1 upcoming appointment/)).toBeInTheDocument()
})

test('history labels render for both the old and the new holder', async () => {
  const oldHolder = {
    ...PURCHASE,
    held_by_viewer: false,
    current_holder_id: 'c2',
    current_holder_name: 'Sam Lee',
    transfers: [
      {
        id: 'pt1',
        from_customer_id: 'c1',
        from_customer_name: 'Priya Nair',
        to_customer_id: 'c2',
        to_customer_name: 'Sam Lee',
        reason: 'Client moved',
        override: false,
        transferred_at: '2026-09-20T10:00:00Z',
      },
    ],
    credits: [{ ...PURCHASE.credits[0], used_by_you: 3 }],
  }
  fake({
    purchases: [oldHolder],
    capabilities: ['customers.view', 'billing.view', 'billing.manage'],
    mode: 'admin',
  })
  renderApp('/clients/c1')

  const table = await screen.findByRole('table', { name: 'Packages' })
  expect(within(table).getByText('Transferred to Sam Lee')).toBeInTheDocument()
  expect(
    within(table).getByText(/Purchased by Priya Nair.*transferred to Sam Lee/),
  ).toBeInTheDocument()
  expect(within(table).getByText('Massage: 3 used')).toBeInTheDocument()
})

test('the new holder sees Received from', async () => {
  const newHolder = {
    ...PURCHASE,
    id: 'pp3',
    held_by_viewer: true,
    purchaser_name: 'Priya Nair',
    current_holder_id: 'c1',
    current_holder_name: 'Priya Nair',
    transfers: [
      {
        id: 'pt1',
        from_customer_id: 'c9',
        from_customer_name: 'Ana Woo',
        to_customer_id: 'c1',
        to_customer_name: 'Priya Nair',
        reason: 'Client moved',
        override: false,
        transferred_at: '2026-09-20T10:00:00Z',
      },
    ],
  }
  fake({
    purchases: [newHolder],
    capabilities: ['customers.view', 'billing.view', 'billing.manage'],
    mode: 'admin',
  })
  renderApp('/clients/c1')

  const table = await screen.findByRole('table', { name: 'Packages' })
  expect(within(table).getByText('Received from Ana Woo')).toBeInTheDocument()
})

// --- Refund dialog: holder-loses-credits warning (spec #96 story 26) ----------------------------

test('the Refund dialog warns the holder will lose remaining credits, when the holder is not the purchaser', async () => {
  const user = userEvent.setup()
  const transferred = {
    ...PURCHASE,
    current_holder_id: 'c2',
    current_holder_name: 'Sam Lee',
  }
  fake({
    purchases: [transferred],
    capabilities: ['customers.view', 'billing.view', 'billing.manage'],
    mode: 'admin',
  })
  renderApp('/clients/c1')

  await screen.findByRole('table', { name: 'Packages' })
  await user.click(screen.getByRole('button', { name: 'Refund' }))

  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByText(/Sam Lee will lose the 7 remaining credits/)).toBeInTheDocument()
})
