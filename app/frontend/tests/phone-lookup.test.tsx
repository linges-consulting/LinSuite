import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * Phone lookup and the screen-pop panel (Phase 14, #16). What this file pins down: the real
 * feature works by typed digits or formatted input, a shared prefix returns several
 * candidates rather than picking one, a single match carries classification and previous-
 * provider history with a quick-book shortcut, and the simulate control (decision #1) is
 * reachable only once `GET /api/cti/demo-mode` says demo mode is on — never rendered, never
 * in the DOM at all, while it is off.
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

const MATCH = {
  status: 'match',
  candidates: [],
  match: {
    customer: {
      id: 'c1',
      first_name: 'Priya',
      last_name: 'Nair',
      email: null,
      phone: '4165550199',
      classification: 'vip',
      created_at: '2026-01-01T00:00:00Z',
    },
    previous_providers: [
      {
        staff_id: 's1',
        display_name: 'Ana Rossi',
        colour: 'blue',
        service_name: 'Swedish Massage',
        last_visit_at: '2026-05-01T14:00:00Z',
      },
    ],
  },
}

const CANDIDATES = {
  status: 'candidates',
  candidates: [
    { id: 'c1', first_name: 'Priya', last_name: 'Nair', email: null, phone: '4165550100', classification: 'new', created_at: '2026-01-01T00:00:00Z' },
    { id: 'c2', first_name: 'Femi', last_name: 'Okoye', email: null, phone: '4165550101', classification: 'new', created_at: '2026-01-01T00:00:00Z' },
  ],
  match: null,
}

const NO_MATCH = { status: 'no_match', candidates: [], match: null }

function ctiStub(opts: {
  capabilities?: string[]
  demoMode?: boolean
  lookup?: Record<string, unknown>
  simulate?: { status: number; body: unknown }
} = {}) {
  const { capabilities = ['customers.view'], demoMode = false, lookup = {}, simulate } = opts
  const calls: { url: string; method: string }[] = []
  const api = stubApi({
    signedIn: true,
    respond: (url: string, body: any) => {
      calls.push({ url, method: body === undefined ? 'GET' : 'POST' })
      if (url === '/api/auth/me') return Response.json(ACCOUNT(capabilities))
      const parsed = new URL(url, 'http://test')
      if (parsed.pathname === '/api/cti/demo-mode') return Response.json({ enabled: demoMode })
      if (parsed.pathname === '/api/cti/lookup') {
        const phone = parsed.searchParams.get('phone') ?? ''
        return Response.json(lookup[phone] ?? NO_MATCH)
      }
      if (parsed.pathname === '/api/cti/simulate-call') {
        if (simulate) return Response.json(simulate.body, { status: simulate.status })
        return Response.json({ call_id: 'call-1', phone: '4165550199', received_at: '2026-09-29T12:00:00Z' })
      }
      return undefined
    },
  })
  return { ...api, calls }
}

// --- nav gating -------------------------------------------------------------------------------

test('the Phone Lookup nav entry is offered to an account holding customers.view', async () => {
  ctiStub()
  renderApp('/')

  expect(await screen.findByRole('link', { name: 'Phone Lookup' })).toBeInTheDocument()
})

test('without customers.view, there is no Phone Lookup nav entry', async () => {
  ctiStub({ capabilities: ['schedule.view'] })
  renderApp('/')

  await screen.findByRole('link', { name: 'Schedule' })
  await waitFor(() => expect(screen.queryByRole('link', { name: 'Phone Lookup' })).not.toBeInTheDocument())
})

// --- the lookup itself -------------------------------------------------------------------------

test('fewer than four digits asks for more and makes no request', async () => {
  const server = ctiStub()
  const user = userEvent.setup()
  renderApp('/phone-lookup')

  await user.type(await screen.findByLabelText("Caller's phone number"), '416')

  expect(await screen.findByText('Keep typing…')).toBeInTheDocument()
  expect(server.calls.some((c) => c.url.startsWith('/api/cti/lookup'))).toBe(false)
})

test('an unmatched number says so plainly', async () => {
  ctiStub({ lookup: { '6045550100': NO_MATCH } })
  const user = userEvent.setup()
  renderApp('/phone-lookup')

  await user.type(await screen.findByLabelText("Caller's phone number"), '604-555-0100')

  expect(await screen.findByText('No client matches that number.')).toBeInTheDocument()
})

test('a formatted number is sent to the server as digits, unstripped of its country code', async () => {
  // The client only strips formatting (`typed.replace(/\D/g, '')`, `routes/phone-lookup.tsx`)
  // — NANP `+1`/leading-`1` normalisation is the server's job (`customers/phone.py`), proven
  // at S1 in `tests/test_cti.py`. This is the wire contract: whatever digits the field holds,
  // untouched, is what gets sent.
  const server = ctiStub({ lookup: { '14165550199': MATCH } })
  const user = userEvent.setup()
  renderApp('/phone-lookup')

  await user.type(await screen.findByLabelText("Caller's phone number"), '+1 (416) 555-0199')

  expect(await screen.findByText('Priya Nair')).toBeInTheDocument()
  expect(server.calls.some((c) => c.url === '/api/cti/lookup?phone=14165550199')).toBe(true)
})

test('several candidates sharing a prefix are all listed, not picked for the caller', async () => {
  ctiStub({ lookup: { '4165550': CANDIDATES } })
  const user = userEvent.setup()
  renderApp('/phone-lookup')

  await user.type(await screen.findByLabelText("Caller's phone number"), '4165550')

  expect(await screen.findByText('Priya Nair')).toBeInTheDocument()
  expect(screen.getByText('Femi Okoye')).toBeInTheDocument()
})

test('a single match shows classification, previous providers and a quick-book shortcut', async () => {
  ctiStub({ lookup: { '4165550199': MATCH } })
  const user = userEvent.setup()
  renderApp('/phone-lookup')

  await user.type(await screen.findByLabelText("Caller's phone number"), '4165550199')

  expect(await screen.findByText('Priya Nair')).toBeInTheDocument()
  expect(screen.getByText('VIP')).toBeInTheDocument()
  expect(screen.getByText(/Seen before by Ana Rossi/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Book appointment' })).toBeInTheDocument()
})

// --- demo mode: the simulate control's own reachability ----------------------------------------

test('with demo mode off, the simulate control is not in the document at all', async () => {
  ctiStub({ demoMode: false })
  renderApp('/phone-lookup')

  await screen.findByLabelText("Caller's phone number")
  await waitFor(() =>
    expect(screen.queryByRole('button', { name: /simulate incoming call/i })).not.toBeInTheDocument(),
  )
})

test('with demo mode on, simulating a call surfaces it on the screen-pop panel', async () => {
  ctiStub({ demoMode: true, lookup: { '4165550199': MATCH } })
  const user = userEvent.setup()
  renderApp('/phone-lookup')

  const button = await screen.findByRole('button', { name: /simulate incoming call/i })
  await user.click(button)

  expect(await screen.findByText('Incoming call')).toBeInTheDocument()
  expect(await screen.findByText('Priya Nair')).toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Dismiss' }))
  await waitFor(() => expect(screen.queryByText('Incoming call')).not.toBeInTheDocument())
})
