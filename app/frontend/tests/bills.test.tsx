import { screen, waitFor, within } from '@testing-library/react'
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
    prepaid_cents: 0,
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
    updated_at: '2026-09-27T14:00:00Z',
    lines: [line()],
    eligible_discounts: [DISCOUNT],
    subtotal_cents: 12000,
    discount_total_cents: 0,
    tax_totals_by_component: {},
    tax_total_cents: 0,
    grand_total_cents: 12000,
    // #64: off by default in these fixtures (even though the real server defaults both on),
    // so every pre-existing test above is unaffected — a test about either override path
    // turns its own toggle on explicitly.
    override_total_cents: null,
    override_reason: null,
    bill_override_requests_enabled: false,
    inline_admin_bill_edit_enabled: false,
    // #107: null for an ordinary draft — set only while this draft is a cancel & replace
    // reopening.
    replaces_invoice_id: null,
    ...overrides,
  }
}

function billsStub(
  opts: {
    capabilities?: string[]
    /** #114: `billing.manage` is administrative — deciding an override request needs this
     *  session actually in Admin Mode, not just holding the capability. */
    mode?: 'staff' | 'admin'
    bill?: ReturnType<typeof bill>
    applyResponse?: { status: number; body: unknown }
    /** #107: the cancelled invoice `bill.replaces_invoice_id` points at — the "Replaces #N"
     *  banner's own fallback read (`GET /api/invoices/{id}`) when there is no router state. */
    replacesInvoice?: { id: string; invoice_number: number; cancel_reason: string }
  } = {},
) {
  const { capabilities = ['billing.view'], mode = 'staff', applyResponse, replacesInvoice } = opts
  let current = opts.bill ?? bill()
  const applied: string[][] = []
  const requests: any[] = []
  let requestSeq = 0
  const inlineAdmin = { active: false, admin_email: null as string | null, hard_limit_at: null as string | null }
  const requestOut = (r: any) => ({
    id: r.id,
    bill_id: current.id,
    kind: r.kind,
    reason: r.reason,
    requested_total_cents: r.requested_total_cents,
    requested_by_email: 'desk@cedar.example',
    requested_at: '2026-09-27T14:05:00Z',
    bill_revision_as_of: current.updated_at,
    status: r.status,
    decided_by_email: r.decided_by_email ?? null,
    decided_at: r.decided_at ?? null,
    decision_note: r.decision_note ?? null,
    decided_total_cents: r.decided_total_cents ?? null,
  })
  const api = stubApi({
    signedIn: true,
    respond: (url: string, body: any) => {
      const parsed = new URL(url, 'http://test')
      if (url === '/api/auth/me') return Response.json(ACCOUNT(capabilities, mode))
      if (!capabilities.includes('billing.view') && parsed.pathname.startsWith('/api/bills')) {
        return Response.json(
          { detail: "You don't hold a capability this needs.", code: 'capability_required' },
          { status: 403 },
        )
      }
      const overrideRequestsMatch = parsed.pathname.match(/^\/api\/bills\/([^/]+)\/override-requests$/)
      if (overrideRequestsMatch) {
        // GET (list) carries no body here, the same "presence of `body`" convention the
        // discounts handling above already relies on.
        if (body === undefined) return Response.json({ requests: requests.map(requestOut) })
        requestSeq += 1
        const created = { id: `r${requestSeq}`, status: 'pending', ...body }
        requests.push(created)
        return Response.json(requestOut(created), { status: 201 })
      }
      const decisionMatch = parsed.pathname.match(
        /^\/api\/bills\/([^/]+)\/override-requests\/([^/]+)\/decision$/,
      )
      if (decisionMatch) {
        const target = requests.find((r) => r.id === decisionMatch[2])
        if (!target) return Response.json({ detail: 'No such override request.' }, { status: 404 })
        target.status = body.decision
        target.decided_by_email = 'owner@cedar.example'
        if (body.decision === 'approved') {
          target.decided_total_cents = body.decided_total_cents ?? target.requested_total_cents
          current = { ...current, override_total_cents: target.decided_total_cents }
        }
        return Response.json(requestOut(target))
      }
      const inlineAuthMatch = parsed.pathname === `/api/bills/${current.id}/inline-admin/authenticate`
      if (inlineAuthMatch) {
        if (body.password !== 'correct horse battery') {
          return Response.json(
            { detail: 'Incorrect email or password', code: 'invalid_password' },
            { status: 403 },
          )
        }
        inlineAdmin.active = true
        inlineAdmin.admin_email = body.email
        inlineAdmin.hard_limit_at = new Date(Date.now() + 15 * 60_000).toISOString()
        return Response.json(inlineAdmin)
      }
      const inlineReleaseMatch = parsed.pathname === `/api/bills/${current.id}/inline-admin/release`
      if (inlineReleaseMatch) {
        inlineAdmin.active = false
        return new Response(null, { status: 204 })
      }
      const inlineOverrideMatch = parsed.pathname === `/api/bills/${current.id}/inline-admin/override`
      if (inlineOverrideMatch) {
        if (!inlineAdmin.active) {
          return Response.json(
            { detail: 'An admin/owner must authenticate first.', code: 'inline_admin_authority_required' },
            { status: 403 },
          )
        }
        inlineAdmin.active = false
        current = {
          ...current,
          override_total_cents: body.total_cents,
          override_reason: body.reason,
        }
        return Response.json(current)
      }
      const inlineStatusMatch = parsed.pathname === `/api/bills/${current.id}/inline-admin`
      if (inlineStatusMatch) return Response.json(inlineAdmin)
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
      if (replacesInvoice && parsed.pathname === `/api/invoices/${replacesInvoice.id}`) {
        return Response.json(replacesInvoice)
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

test('the Billing nav entry is offered when billing.view is held', async () => {
  billsStub()
  renderApp('/')

  expect(await screen.findByRole('link', { name: 'Billing' })).toBeInTheDocument()
})

test('without billing.view there is no Billing nav entry', async () => {
  billsStub({ capabilities: ['schedule.view'] })
  renderApp('/')

  await screen.findByRole('link', { name: 'Schedule' })
  await waitFor(() => expect(screen.queryByRole('link', { name: 'Billing' })).not.toBeInTheDocument())
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

test('a line paid by a package credit carries a Prepaid badge; a charged one does not', async () => {
  billsStub({
    bill: bill({
      lines: [
        line({ id: 'l1', prepaid_cents: 9600, service: { id: 'sv1', name: 'Swedish Massage' } }),
        line({ id: 'l2', appointment_id: 'a2', service: { id: 'sv2', name: 'Hot Stone' } }),
      ],
    }),
  })
  renderApp('/bills/b1')

  const prepaid = (await screen.findByText('Swedish Massage')).closest('tr')!
  expect(within(prepaid).getByText('Prepaid')).toBeInTheDocument()
  const charged = screen.getByText('Hot Stone').closest('tr')!
  expect(within(charged).queryByText('Prepaid')).not.toBeInTheDocument()
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

// --- bill review authority (#64) ------------------------------------------------------------

test('neither override path is offered when both toggles are off', async () => {
  billsStub()
  renderApp('/bills/b1')

  await screen.findByRole('heading', { name: 'Priya Nair' })
  expect(screen.queryByRole('button', { name: 'Request an exception' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Sign in as admin' })).not.toBeInTheDocument()
})

test('staff can submit an exception request and it shows as pending', async () => {
  const user = userEvent.setup()
  billsStub({ bill: bill({ bill_override_requests_enabled: true }) })
  renderApp('/bills/b1')

  await user.click(await screen.findByRole('button', { name: 'Request an exception' }))
  await user.type(await screen.findByLabelText('Proposed total'), '95')
  await user.type(screen.getByLabelText('Reason'), 'Loyal client, matched a competitor offer')
  await user.click(screen.getByRole('button', { name: 'Submit' }))

  expect(await screen.findByText('pending')).toBeInTheDocument()
  expect(screen.getByText('Ad hoc discount to', { exact: false })).toBeInTheDocument()
  expect(screen.getByText('$95.00')).toBeInTheDocument()
})

test('an admin/owner reviewing can approve a pending request', async () => {
  const user = userEvent.setup()
  const seeded = bill({ bill_override_requests_enabled: true })
  const api = billsStub({
    capabilities: ['billing.view', 'billing.manage'],
    mode: 'admin',
    bill: seeded,
  })
  renderApp('/bills/b1')
  await user.click(await screen.findByRole('button', { name: 'Request an exception' }))
  await user.type(await screen.findByLabelText('Proposed total'), '95')
  await user.type(screen.getByLabelText('Reason'), 'Goodwill')
  await user.click(screen.getByRole('button', { name: 'Submit' }))
  await screen.findByText('pending')

  await user.click(screen.getByRole('button', { name: 'Approve' }))

  await waitFor(() => expect(screen.getByText('approved')).toBeInTheDocument())
  expect(screen.getByText('Admin-authorized total')).toBeInTheDocument()
  expect(api.calls.some((c) => c.url.endsWith('/decision') && c.method === 'POST')).toBe(true)
})

// #114 (spec #113 Staff Mode section): `billing.manage` is administrative, so holding it is
// not enough on its own — deciding a request needs the session in Admin Mode too, or the
// buttons offer an action `decideOverrideRequest` would refuse.
test('holding billing.manage in Staff Mode does not offer Approve or Reject on a pending request', async () => {
  const user = userEvent.setup()
  billsStub({
    capabilities: ['billing.view', 'billing.manage'],
    mode: 'staff',
    bill: bill({ bill_override_requests_enabled: true }),
  })
  renderApp('/bills/b1')

  await user.click(await screen.findByRole('button', { name: 'Request an exception' }))
  await user.type(await screen.findByLabelText('Proposed total'), '95')
  await user.type(screen.getByLabelText('Reason'), 'Goodwill')
  await user.click(screen.getByRole('button', { name: 'Submit' }))

  await screen.findByText('pending')
  expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Reject' })).not.toBeInTheDocument()
})

test('an admin/owner can authenticate inline and an edit ends the window on save', async () => {
  const user = userEvent.setup()
  billsStub({ bill: bill({ inline_admin_bill_edit_enabled: true }) })
  renderApp('/bills/b1')

  await user.click(await screen.findByRole('button', { name: 'Sign in as admin' }))
  await user.type(await screen.findByLabelText('Email'), 'owner@cedar.example')
  await user.type(screen.getByLabelText('Password'), 'correct horse battery')
  await user.click(screen.getByRole('button', { name: 'Continue' }))

  expect(await screen.findByText(/Editing as/)).toBeInTheDocument()
  await user.type(await screen.findByLabelText('New total'), '75')
  await user.type(screen.getByLabelText('Reason'), 'Goodwill correction')
  await user.click(screen.getByRole('button', { name: 'Save' }))

  // "Ends on save": back to the offer-to-sign-in state, no lingering editing banner.
  await waitFor(() =>
    expect(screen.queryByText(/Editing as/)).not.toBeInTheDocument(),
  )
  expect(await screen.findByRole('button', { name: 'Sign in as admin' })).toBeInTheDocument()
})

test('a wrong admin password on the inline auth dialog is refused', async () => {
  const user = userEvent.setup()
  billsStub({ bill: bill({ inline_admin_bill_edit_enabled: true }) })
  renderApp('/bills/b1')

  await user.click(await screen.findByRole('button', { name: 'Sign in as admin' }))
  await user.type(await screen.findByLabelText('Email'), 'owner@cedar.example')
  await user.type(screen.getByLabelText('Password'), 'not the password')
  await user.click(screen.getByRole('button', { name: 'Continue' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('Incorrect email or password')
})

// --- cancel & replace's "Replaces #N" banner (#107) ------------------------------------------

test('a reopened draft with no router state fetches the cancelled original for its Replaces banner', async () => {
  // The direct-link/reload case: no `navigate(..., { state })` to read, so the banner comes
  // from `bill.replaces_invoice_id` (#107's own backend field) plus a fetch of that invoice.
  billsStub({
    bill: bill({ replaces_invoice_id: 'inv1' }),
    replacesInvoice: { id: 'inv1', invoice_number: 42, cancel_reason: 'Booked the wrong service' },
  })
  renderApp('/bills/b1')

  await screen.findByRole('heading', { name: 'Priya Nair' })
  expect(await screen.findByText('Replaces #42')).toBeInTheDocument()
  expect(screen.getByText('Booked the wrong service')).toBeInTheDocument()
})

test('an ordinary draft bill shows no Replaces banner', async () => {
  billsStub()
  renderApp('/bills/b1')

  await screen.findByRole('heading', { name: 'Priya Nair' })
  expect(screen.queryByText(/^Replaces #/)).not.toBeInTheDocument()
})
