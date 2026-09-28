import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * The walk-in queue display (Phase 7 Task 8, #12): the nav entry only appears once
 * `enable_walk_in_queue` is on *and* the account holds `queue.manage` (Task 1's own
 * acceptance criterion — "no queue surface exists anywhere in the product" while the toggle
 * is off — applied to the nav now that a real screen exists to hide); the list itself with
 * its wait estimate (Task 6, always worded as an estimate) and compliance-gap indicator
 * (Task 7); adding a walk-in; and the two different 422 reasons a start can be refused for
 * (Task 4/5's `not_eligible`/`staff_busy` — the literal "cannot run into a booked appointment"
 * criterion — versus a separate `resource_unavailable`).
 *
 * `respond`'s `body` argument distinguishes GET from POST at the same URL (`undefined` for a
 * GET, an object — even `{}` — for a POST), the same trick `compliance.test.tsx` already
 * uses; `fetchQueueEntries` always asks with `include_abandoned=true` (`lib/api.ts`), so every
 * GET here is the one shape.
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

const SERVICE = { id: 'sv1', name: 'Haircut', duration_minutes: 30, staff_ids: ['st1'] }
const STAFF = { id: 'st1', display_name: 'Ana Rossi', colour: 'blue', hex: '#000', dark_hex: '#fff', sort_order: 0, user_id: 'u1' }

function entry(overrides: Record<string, unknown> = {}) {
  return {
    id: 'q1',
    status: 'waiting',
    arrived_at: '2026-09-27T14:00:00Z',
    requested_service: { id: SERVICE.id, name: SERVICE.name },
    preferred_staff: null,
    customer: null,
    bare_name: 'Jamie Lee',
    bare_phone: null,
    appointment_id: null,
    estimated_wait_minutes: 12,
    compliance_gaps: [],
    ...overrides,
  }
}

function queueStub(opts: {
  capabilities?: string[]
  featureOn?: boolean
  entries?: Record<string, unknown>[]
  startResponse?: { status: number; body: unknown }
} = {}) {
  const { capabilities = ['queue.manage'], featureOn = true, entries: initial = [], startResponse } = opts
  let list = [...initial]
  const added: Record<string, unknown>[] = []
  const api = stubApi({
    signedIn: true,
    respond: (url: string, body: any) => {
      const parsed = new URL(url, 'http://test')
      if (url === '/api/auth/me') return Response.json(ACCOUNT(capabilities))
      // `Requires("queue.manage")` refuses before `_feature_gate` ever runs (`scheduling/
      // queue.py`) — every queue route in this fake does the same, capability first.
      if (
        parsed.pathname.startsWith('/api/queue-entries') &&
        !capabilities.includes('queue.manage')
      ) {
        return Response.json(
          { detail: "You don't hold a capability this needs.", code: 'capability_required' },
          { status: 403 },
        )
      }
      if (parsed.pathname === '/api/queue-entries') {
        if (!featureOn) return Response.json({ detail: 'Not found.' }, { status: 404 })
        if (body) {
          added.push(body)
          const created = entry({
            id: `q${list.length + 1}`,
            bare_name: body.bare_name ?? null,
            bare_phone: body.bare_phone ?? null,
            customer: body.customer_id ? { id: body.customer_id, first_name: 'Priya', last_name: 'Nair', phone: null } : null,
            preferred_staff: body.preferred_staff_id ? { id: STAFF.id, display_name: STAFF.display_name } : null,
            estimated_wait_minutes: null,
            compliance_gaps: null,
          })
          list = [...list, created]
          return Response.json(created, { status: 201 })
        }
        return Response.json({ entries: list })
      }
      const abandonMatch = parsed.pathname.match(/^\/api\/queue-entries\/([^/]+)\/abandon$/)
      if (abandonMatch) {
        const id = abandonMatch[1]
        list = list.map((e) => (e.id === id ? { ...e, status: 'abandoned', estimated_wait_minutes: null } : e))
        return Response.json(list.find((e) => e.id === id))
      }
      const startMatch = parsed.pathname.match(/^\/api\/queue-entries\/([^/]+)\/start$/)
      if (startMatch) {
        if (startResponse) return Response.json(startResponse.body, { status: startResponse.status })
        const id = startMatch[1]
        list = list.map((e) => (e.id === id ? { ...e, status: 'in_service', appointment_id: 'appt1' } : e))
        return Response.json(
          {
            queue_entry: list.find((e) => e.id === id),
            appointment: { id: 'appt1', staff: { display_name: STAFF.display_name }, service: { name: SERVICE.name } },
          },
          { status: 201 },
        )
      }
      if (url === '/api/catalog/services') return Response.json({ services: [SERVICE] })
      if (url === '/api/staff') return Response.json({ staff: [STAFF] })
      return undefined
    },
  })
  return { ...api, added, list: () => list }
}

async function selectOption(user: ReturnType<typeof userEvent.setup>, comboboxName: string, optionName: string | RegExp) {
  const trigger = await screen.findByRole('combobox', { name: comboboxName })
  trigger.focus()
  await user.keyboard('{ArrowDown}')
  await user.click(await screen.findByRole('option', { name: optionName }))
}

// --- nav gating -----------------------------------------------------------------------------

test('the Queue nav entry is offered once the toggle is on and the capability is held', async () => {
  queueStub({ entries: [entry()] })
  renderApp('/')

  expect(await screen.findByRole('link', { name: 'Queue' })).toBeInTheDocument()
})

test('without queue.manage, there is no Queue nav entry even with the toggle on', async () => {
  queueStub({ capabilities: ['schedule.view'] })
  renderApp('/')

  await screen.findByRole('link', { name: 'Schedule' })
  await waitFor(() => expect(screen.queryByRole('link', { name: 'Queue' })).not.toBeInTheDocument())
})

test('with the toggle off, there is no Queue nav entry, and the screen explains it directly', async () => {
  queueStub({ featureOn: false })
  renderApp('/queue')

  await waitFor(() => expect(screen.queryByRole('link', { name: 'Queue' })).not.toBeInTheDocument())
  expect(await screen.findByText('Walk-in queue is turned off')).toBeInTheDocument()
})

test('the toggle on but no capability: a direct visit gets a permission message, not a crash', async () => {
  queueStub({ capabilities: ['schedule.view'] })
  renderApp('/queue')

  expect(await screen.findByRole('alert')).toHaveTextContent("You don't have permission to see the queue.")
})

// --- the list ---------------------------------------------------------------------------------

test('lists a waiting entry with its estimate worded as one, and an in-service entry with no estimate', async () => {
  queueStub({
    entries: [
      entry({ id: 'q1', bare_name: 'Jamie Lee', estimated_wait_minutes: 12 }),
      entry({
        id: 'q2',
        status: 'in_service',
        bare_name: 'Sam Osei',
        estimated_wait_minutes: null,
        appointment_id: 'appt9',
      }),
    ],
  })
  renderApp('/queue')

  expect(await screen.findByText('Jamie Lee')).toBeInTheDocument()
  expect(screen.getByText('~12 min wait (estimate)')).toBeInTheDocument()
  expect(screen.getByText('Sam Osei')).toBeInTheDocument()
  expect(screen.getByText('In service')).toBeInTheDocument()
  // 1 waiting, 1 in service, 0 abandoned today.
  expect(screen.getByText('1 waiting · 1 in service · 0 abandoned today')).toBeInTheDocument()
})

test('shows the essential-form gap indicator and its own caveat about what it can and cannot see', async () => {
  queueStub({
    entries: [
      entry({
        compliance_gaps: [{ template_id: 't1', name: 'Massage waiver', status: 'missing' }],
      }),
    ],
  })
  renderApp('/queue')

  expect(await screen.findByText('1 form')).toBeInTheDocument()
  expect(
    screen.getByText(/only reliably shows gaps for essential forms required of every client/),
  ).toBeInTheDocument()
})

test('an abandoned entry from earlier today counts toward "abandoned today" and is not listed', async () => {
  const now = new Date().toISOString()
  queueStub({
    entries: [
      entry({ id: 'q1', status: 'waiting' }),
      entry({ id: 'q2', status: 'abandoned', bare_name: 'Gone Already', arrived_at: now }),
    ],
  })
  renderApp('/queue')

  await screen.findByText('Jamie Lee')
  expect(screen.getByText('1 waiting · 0 in service · 1 abandoned today')).toBeInTheDocument()
  expect(screen.queryByText('Gone Already')).not.toBeInTheDocument()
})

// --- adding -------------------------------------------------------------------------------------

test('adding a walk-in sends its bare name and chosen service, and it appears in the list', async () => {
  const user = userEvent.setup()
  const stub = queueStub({ entries: [] })
  renderApp('/queue')

  await screen.findByText('Nobody waiting')
  await user.click(screen.getByRole('button', { name: 'Add walk-in' }))
  const dialog = await screen.findByRole('dialog', { name: 'Add to the queue' })
  await selectOption(user, 'Service', /Haircut/)
  await user.type(within(dialog).getByLabelText('Name'), 'Morgan Kim')
  await user.click(within(dialog).getByRole('button', { name: 'Add to queue' }))

  await waitFor(() => expect(stub.added).toEqual([
    { requested_service_id: 'sv1', preferred_staff_id: null, bare_name: 'Morgan Kim', bare_phone: null },
  ]))
  expect(await screen.findByText('Morgan Kim')).toBeInTheDocument()
})

// --- starting ------------------------------------------------------------------------------------

test('a walk-in that would run into a booked appointment gets a clear, specific reason', async () => {
  const user = userEvent.setup()
  queueStub({
    entries: [entry()],
    startResponse: {
      status: 422,
      body: { detail: 'This walk-in cannot be started right now.', code: 'not_eligible', reason: 'staff_busy' },
    },
  })
  renderApp('/queue')

  await user.click(await screen.findByRole('button', { name: 'Start' }))

  expect(await screen.findByText("Can't start yet — busy right now.")).toBeInTheDocument()
  // Refused, not silently dropped — the entry is still there to try again.
  expect(screen.getByText('Jamie Lee')).toBeInTheDocument()
})

test('a busy room or device is shown as its own, separate reason', async () => {
  const user = userEvent.setup()
  queueStub({
    entries: [entry()],
    startResponse: {
      status: 422,
      body: { detail: 'No free room or device for this service right now.', code: 'resource_unavailable' },
    },
  })
  renderApp('/queue')

  await user.click(await screen.findByRole('button', { name: 'Start' }))

  expect(await screen.findByText('No free room or device for this service right now.')).toBeInTheDocument()
})

test('starting a walk-in moves it out of the waiting list', async () => {
  const user = userEvent.setup()
  queueStub({ entries: [entry()] })
  renderApp('/queue')

  await user.click(await screen.findByRole('button', { name: 'Start' }))

  await waitFor(() => expect(screen.getByText('In service')).toBeInTheDocument())
  expect(screen.queryByRole('button', { name: 'Start' })).not.toBeInTheDocument()
})

// --- abandoning ------------------------------------------------------------------------------

test('abandoning a waiting entry removes it from the visible list', async () => {
  // `arrived_at` must be "now", not the shared `entry()` factory's fixed default — abandoning
  // sets `status` only (this file's mock, `queueStub`), and "abandoned today" is judged by
  // arrival date (`routes/queue.tsx`'s own `abandonedToday`), so a fixed past date would only
  // count as "today" on the one calendar day this test was written.
  const user = userEvent.setup()
  queueStub({ entries: [entry({ arrived_at: new Date().toISOString() })] })
  renderApp('/queue')

  await screen.findByText('Jamie Lee')
  await user.click(screen.getByRole('button', { name: 'Abandon' }))

  await waitFor(() => expect(screen.queryByText('Jamie Lee')).not.toBeInTheDocument())
  expect(screen.getByText('0 waiting · 0 in service · 1 abandoned today')).toBeInTheDocument()
})
