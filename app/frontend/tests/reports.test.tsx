import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * Reports (#97/#95/#110): a nav entry shown only to an account holding `commission.view` or
 * `billing.manage`, with Commission (date range + staff filter, per-staff totals and the
 * lines behind them) and Package liability (client filter, outstanding credits and their
 * value) tabs — each with CSV export via the shared `ExportControl`. Spec #95 user story 2:
 * "Reports appears only when I can use it, so that staff are not shown a door that is not
 * theirs"; stories 67-71 are the reports themselves.
 */

afterEach(() => vi.unstubAllGlobals())

const ACCOUNT = (capabilities: string[], mode: 'admin' | 'staff' = 'admin') => ({
  id: 'u2',
  email: 'owner@cedar.example',
  role: 'Administrator',
  capabilities,
  mode,
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
})

const COMMISSION_ROW = {
  id: 'p1',
  posted_at: '2026-09-01T12:00:00Z',
  kind: 'earned',
  source: 'service',
  staff_id: 's1',
  staff_name: 'Dana Price',
  invoice_id: 'inv1',
  invoice_number: 101,
  invoice_status: 'issued',
  service_id: 'svc1',
  variant_id: null,
  commission_rate_bp: 1000,
  basis_cents: 10_000,
  amount_cents: 1_000,
  payment_status: 'received',
  invoice_received_cents: 10_000,
  invoice_pending_cents: 0,
  commission_received_cents: 1_000,
  commission_pending_cents: 0,
}

const COMMISSION_REPORT = {
  rows: [COMMISSION_ROW],
  service: { revenue_cents: 10_000, commission_cents: 1_000, commission_received_cents: 1_000, commission_pending_cents: 0 },
  retail: { revenue_cents: 0, commission_cents: 0, commission_received_cents: 0, commission_pending_cents: 0 },
  total_earned_cents: 1_000,
  total_received_cents: 1_000,
  total_pending_cents: 0,
  payments_received_cents: 10_000,
  payments_pending_cents: 0,
  from: '2026-06-01',
  to: '2026-09-29',
  timezone: 'America/Toronto',
}

const LIABILITY_ROW = {
  customer_id: 'c1',
  customer_name: 'Alex River',
  package_purchase_id: 'pp1',
  package_name: '10-pack massage',
  purchased_at: '2026-05-01T12:00:00Z',
  expires_at: '2027-05-01',
  service_id: 'svc1',
  service_name: 'Massage',
  credits_total: 10,
  credits_redeemed: 3,
  credits_remaining: 7,
  unused_value_cents: 70_000,
}

const LIABILITY_REPORT = {
  rows: [LIABILITY_ROW],
  customers: [{ customer_id: 'c1', customer_name: 'Alex River', credits_remaining: 7, unused_value_cents: 70_000 }],
  total_unused_value_cents: 70_000,
  as_of: '2026-09-29',
  from: null,
  to: null,
  timezone: 'America/Toronto',
}

const ROSTER = { staff: [{ id: 's1', display_name: 'Dana Price', colour: 'blue', hex: '#1', dark_hex: '#2', sort_order: 1, user_id: 'u10' }] }

function reportsStub(capabilities: string[], mode: 'admin' | 'staff' = 'admin') {
  return stubApi({
    signedIn: true,
    respond: (url) => {
      if (url === '/api/auth/me') return Response.json(ACCOUNT(capabilities, mode))
      if (url === '/api/staff') return Response.json(ROSTER)
      if (url.startsWith('/api/admin/reports/commission/exports')) {
        return Response.json(
          { id: 'e1', status: 'pending', from: '2026-06-01', to: '2026-09-29', staff_id: null, created_at: '2026-09-29T00:00:00Z', completed_at: null, download_url: null },
          { status: 202 },
        )
      }
      if (url.startsWith('/api/admin/reports/commission')) return Response.json(COMMISSION_REPORT)
      if (url.startsWith('/api/admin/reports/package-liability/exports')) {
        return Response.json(
          { id: 'e2', status: 'pending', from: null, to: null, customer_id: null, created_at: '2026-09-29T00:00:00Z', completed_at: null, download_url: null },
          { status: 202 },
        )
      }
      if (url.startsWith('/api/admin/reports/package-liability')) return Response.json(LIABILITY_REPORT)
      if (url.startsWith('/api/customers')) return Response.json({ customers: [], total: 0 })
      return undefined
    },
  })
}

test('Reports is offered to an account holding commission.view', async () => {
  reportsStub(['commission.view'])
  renderApp('/')

  expect(await screen.findByRole('link', { name: 'Reports' })).toBeInTheDocument()
})

test('Reports is offered to an account holding billing.manage', async () => {
  reportsStub(['billing.manage'])
  renderApp('/')

  expect(await screen.findByRole('link', { name: 'Reports' })).toBeInTheDocument()
})

test('Reports is hidden from an account holding neither', async () => {
  reportsStub(['schedule.view'])
  renderApp('/')

  await screen.findByRole('link', { name: 'Schedule' })
  await waitFor(() => expect(screen.queryByRole('link', { name: 'Reports' })).not.toBeInTheDocument())
})

test('an account holding only commission.view sees only the Commission tab', async () => {
  reportsStub(['commission.view'])
  renderApp('/reports')

  expect(await screen.findByRole('tab', { name: 'Commission', selected: true })).toBeInTheDocument()
  expect(screen.queryByRole('tab', { name: 'Package liability' })).not.toBeInTheDocument()
})

test('an account holding only billing.manage sees only the Package liability tab', async () => {
  reportsStub(['billing.manage'])
  renderApp('/reports')

  expect(await screen.findByRole('tab', { name: 'Package liability', selected: true })).toBeInTheDocument()
  expect(screen.queryByRole('tab', { name: 'Commission' })).not.toBeInTheDocument()
})

// #114 (spec #113 Staff Mode section): `/reports` is a whole Admin-only route now, guarded by
// `RequireAdminMode` in `App.tsx` — a Staff Mode visit never reaches `ReportsPage` at all, so
// it shows the shared needs-Admin-Mode page (with the mode switcher) rather than its own
// capability-flavoured empty state.
test('opening Reports in Staff Mode shows the needs-Admin-Mode page instead of the tabs', async () => {
  reportsStub(['commission.view', 'billing.manage'], 'staff')
  renderApp('/reports')

  expect(await screen.findByText('This area needs Admin Mode')).toBeInTheDocument()
  // The shell's own header switcher plus the guard page's — both are the same control.
  expect(screen.getAllByLabelText(/Switch mode/).length).toBeGreaterThan(0)
  expect(screen.queryByRole('tab', { name: 'Commission' })).not.toBeInTheDocument()
  expect(screen.queryByRole('tab', { name: 'Package liability' })).not.toBeInTheDocument()
})

test('Commission renders per-staff totals, the underlying lines and an export control', async () => {
  reportsStub(['commission.view', 'billing.manage'])
  renderApp('/reports')

  await screen.findByRole('tab', { name: 'Commission', selected: true })
  expect((await screen.findAllByText('Dana Price')).length).toBeGreaterThan(0)
  expect(screen.getAllByText('$10.00').length).toBeGreaterThan(0)
  expect(screen.getByText('#101')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Export CSV' })).toBeInTheDocument()
})

test('requesting the Commission export posts to the commission export endpoint', async () => {
  const user = userEvent.setup()
  const { calls } = reportsStub(['commission.view'])
  renderApp('/reports')

  await user.click(await screen.findByRole('button', { name: 'Export CSV' }))

  await waitFor(() =>
    expect(calls.some((c) => c.method === 'POST' && c.url.startsWith('/api/admin/reports/commission/exports'))).toBe(true),
  )
  expect(await screen.findByText('Preparing…')).toBeInTheDocument()
})

test('switching to Package liability shows its totals, lines and export control', async () => {
  const user = userEvent.setup()
  reportsStub(['billing.manage'])
  renderApp('/reports')

  await user.click(await screen.findByRole('tab', { name: 'Package liability' }))

  expect(await screen.findAllByText('Alex River')).not.toHaveLength(0)
  expect(screen.getAllByText('$700.00').length).toBeGreaterThan(0)
  expect(screen.getByText('10-pack massage')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Export CSV' })).toBeInTheDocument()
})

test('requesting the Package liability export posts to the liability export endpoint', async () => {
  const user = userEvent.setup()
  const { calls } = reportsStub(['billing.manage'])
  renderApp('/reports')

  await user.click(await screen.findByRole('button', { name: 'Export CSV' }))

  await waitFor(() =>
    expect(
      calls.some((c) => c.method === 'POST' && c.url.startsWith('/api/admin/reports/package-liability/exports')),
    ).toBe(true),
  )
})

test('filtering Package liability by client narrows the search picker', async () => {
  const user = userEvent.setup()
  reportsStub(['billing.manage'])
  renderApp('/reports')

  const search = await screen.findByPlaceholderText('All clients')
  await user.type(search, 'Alex')

  expect(search).toHaveValue('Alex')
})
