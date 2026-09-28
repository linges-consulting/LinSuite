import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * The staff-facing draft-bill review screen (#63): the list of visits with a draft bill
 * (#59), a bill's lines with an eligible discount (#58) toggled on and off, tax included in
 * the total (#57), and a rejected discount combination surfacing its specific reason rather
 * than a silent no-op.
 */

afterEach(() => vi.unstubAllGlobals())

const ACCOUNT = (capabilities: string[]) => ({
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

const DISCOUNT = {
  id: 'd1',
  name: 'Autumn 10%',
  kind: 'percentage' as const,
  percentage_bp: 1000,
  amount_cents: null,
  stackable: true,
  applied: false,
}

function line(overrides: Record<string, unknown> = {}) {
  return {
    id: 'l1',
    appointment_id: 'a1',
    service: { id: 'sv1', name: 'Swedish Massage' },
    staff: { id: 'st1', name: 'Ana Rossi' },
    price_cents: 12000,
    applied_discount_ids: [],
    discounted_cents: 12000,
    tax: { pretax_cents: 12000, component_cents: {}, tax_cents: 0, total_cents: 12000 },
    line_total_cents: 12000,
    ...overrides,
  }
}

function bill(overrides: Record<string, unknown> = {}) {
  return {
    id: 'b1',
    status: 'draft',
    customer: { id: 'c1', name: 'Priya Nair' },
    booking_group_id: null,
    created_at: '2026-09-27T14:00:00Z',
    lines: [line()],
    eligible_discounts: [DISCOUNT],
    subtotal_cents: 12000,
    discount_total_cents: 0,
    tax_totals_by_component: {},
    tax_total_cents: 0,
    grand_total_cents: 12000,
    ...overrides,
  }
}

function billsStub(
  opts: {
    capabilities?: string[]
    bill?: ReturnType<typeof bill>
    applyResponse?: { status: number; body: unknown }
  } = {},
) {
  const { capabilities = ['billing.view'], applyResponse } = opts
  let current = opts.bill ?? bill()
  const applied: string[][] = []
  const api = stubApi({
    signedIn: true,
    respond: (url: string, body: any) => {
      const parsed = new URL(url, 'http://test')
      if (url === '/api/auth/me') return Response.json(ACCOUNT(capabilities))
      if (!capabilities.includes('billing.view') && parsed.pathname.startsWith('/api/bills')) {
        return Response.json(
          { detail: "You don't hold a capability this needs.", code: 'capability_required' },
          { status: 403 },
        )
      }
      if (parsed.pathname === '/api/bills') {
        return Response.json({
          bills: [
            {
              id: current.id,
              customer: current.customer,
              booking_group_id: current.booking_group_id,
              created_at: current.created_at,
              line_count: current.lines.length,
              subtotal_cents: current.subtotal_cents,
            },
          ],
        })
      }
      const discountsMatch = parsed.pathname.match(/^\/api\/bills\/([^/]+)\/discounts$/)
      if (discountsMatch) {
        applied.push(body.discount_ids)
        if (applyResponse) return Response.json(applyResponse.body, { status: applyResponse.status })
        const selected: string[] = body.discount_ids
        const eligible_discounts = current.eligible_discounts.map((d: any) => ({
          ...d,
          applied: selected.includes(d.id),
        }))
        const discounted = selected.length > 0 ? 10800 : 12000
        current = {
          ...current,
          eligible_discounts,
          lines: [
            line({
              applied_discount_ids: selected,
              discounted_cents: discounted,
              tax: { pretax_cents: discounted, component_cents: {}, tax_cents: 0, total_cents: discounted },
              line_total_cents: discounted,
            }),
          ],
          discount_total_cents: 12000 - discounted,
          grand_total_cents: discounted,
        }
        return Response.json(current)
      }
      const billMatch = parsed.pathname.match(/^\/api\/bills\/([^/]+)$/)
      if (billMatch) {
        if (billMatch[1] !== current.id) {
          return Response.json({ detail: 'No such service bill.' }, { status: 404 })
        }
        return Response.json(current)
      }
      return undefined
    },
  })
  return { ...api, applied, bill: () => current }
}

// --- nav gating -----------------------------------------------------------------------------

test('the Bills nav entry is offered when billing.view is held', async () => {
  billsStub()
  renderApp('/')

  expect(await screen.findByRole('link', { name: 'Bills' })).toBeInTheDocument()
})

test('without billing.view there is no Bills nav entry', async () => {
  billsStub({ capabilities: ['schedule.view'] })
  renderApp('/')

  await screen.findByRole('link', { name: 'Schedule' })
  await waitFor(() => expect(screen.queryByRole('link', { name: 'Bills' })).not.toBeInTheDocument())
})

// --- the list and the detail screen ---------------------------------------------------------

test('lists a draft bill and opens it', async () => {
  const user = userEvent.setup()
  billsStub()
  renderApp('/bills')

  const link = await screen.findByRole('link', { name: 'Priya Nair' })
  await user.click(link)

  expect(await screen.findByRole('heading', { name: 'Priya Nair' })).toBeInTheDocument()
  expect(screen.getByText('Swedish Massage')).toBeInTheDocument()
  expect(screen.getByText('Ana Rossi')).toBeInTheDocument()
})

test('shows the draft bill with no discount applied yet', async () => {
  billsStub()
  renderApp('/bills/b1')

  expect(await screen.findByRole('heading', { name: 'Priya Nair' })).toBeInTheDocument()
  expect(screen.getByRole('checkbox', { name: /Autumn 10%/ })).not.toBeChecked()
  expect(screen.getAllByText('$120.00').length).toBeGreaterThan(0)
})

test('applying an eligible discount recomputes the total live', async () => {
  const user = userEvent.setup()
  const api = billsStub()
  renderApp('/bills/b1')

  const checkbox = await screen.findByRole('checkbox', { name: /Autumn 10%/ })
  await user.click(checkbox)

  await waitFor(() => expect(checkbox).toBeChecked())
  await waitFor(() => expect(screen.getAllByText('$108.00').length).toBeGreaterThan(0))
  expect(api.applied).toEqual([['d1']])
})

test('a rejected combination shows the specific reason, not a silent no-op', async () => {
  const user = userEvent.setup()
  billsStub({
    applyResponse: {
      status: 422,
      body: { detail: 'Autumn sale: These discounts together exceed the eligible charge.' },
    },
  })
  renderApp('/bills/b1')

  const checkbox = await screen.findByRole('checkbox', { name: /Autumn 10%/ })
  await user.click(checkbox)

  expect(await screen.findByRole('alert')).toHaveTextContent(
    'Autumn sale: These discounts together exceed the eligible charge.',
  )
  // Rejected — the checkbox reverts rather than showing an applied state the server refused.
  await waitFor(() => expect(checkbox).not.toBeChecked())
})
