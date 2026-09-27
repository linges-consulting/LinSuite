import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient } from '@tanstack/react-query'
import { invalidateScheduling } from '@/lib/query-client'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi, type Call } from './harness'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/**
 * Erasure on the client profile (Task 7, ADR-0001 §3). What is worth pinning: the action is
 * in the profile's overflow menu for an Admin Mode holder of `customers.erase` and nowhere
 * else; the confirm dialog says, before anything is sent, exactly what goes and what stays —
 * which depends on whether the chart is under a retention hold; and afterwards a banner on
 * the profile states what was kept and until when.
 */

const DETAIL = {
  id: 'c1',
  first_name: 'Priya',
  last_name: 'Nair',
  email: 'priya@example.com',
  phone: '4165550199',
  created_at: '2026-01-05T15:00:00Z',
  classification: 'new' as const,
  date_of_birth: '2014-03-14',
  emergency_contact_name: 'Ravi Nair',
  emergency_contact_phone: null,
  emergency_contact_relationship: null,
  secondary_contact_name: null,
  secondary_contact_phone: null,
  secondary_contact_email: null,
  notes: 'Prefers the corner room.',
  updated_at: '2026-01-05T15:00:00Z',
  retention: { status: 'not_held', expires_on: null as string | null },
  suppressed: false,
  erasure: null as object | null,
}

const HELD = { status: 'held', expires_on: '2042-03-14' }

const ERASED_HELD = {
  id: 'r1',
  requested_at: '2026-09-21T15:00:00Z',
  held: true,
  held_until: '2042-03-14',
  held_reason: 'Regulated health record — retained until 14 Mar 2042, then destroyed',
  retained: ['Name', 'Date of birth', 'Visit history', 'Session notes'],
  purged_at: null,
}

const ME = {
  id: 'u1',
  email: 'owner@cedar.example',
  role: 'Administrator',
  capabilities: ['admin', 'customers.view', 'customers.manage', 'customers.erase'],
  mode: 'admin',
  can_switch_modes: true,
  admin_grant_expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
  admin_hard_limit_at: new Date(Date.now() + 30 * 60_000).toISOString(),
  must_change_password: false,
  mfa: {
    enrolled: true,
    method: 'totp',
    pending: false,
    enrolment_required: false,
    verified_at: null,
    email_otp_allowed: false,
  },
}

const STAFF_MODE = { ...ME, mode: 'staff', admin_grant_expires_at: null, admin_hard_limit_at: null }

function visit(id: string, startsInDays: number, status = 'confirmed') {
  const starts = new Date(Date.now() + startsInDays * 86_400_000)
  return {
    id,
    starts_at: starts.toISOString(),
    ends_at: new Date(starts.getTime() + 3_600_000).toISOString(),
    status,
    booking_group_id: null,
    service: { id: 'v1', name: 'Swedish Massage' },
    staff: { id: 's1', display_name: 'Ana Rossi', colour: 'blue' },
  }
}

function fake({
  me = ME as object,
  customer = DETAIL as object,
  appointments = [] as object[],
  erasureAnswer = undefined as (() => Response) | undefined,
} = {}) {
  let current: Record<string, unknown> = { ...customer }
  return stubApi({
    signedIn: true,
    dualRole: false,
    respond: (url: string) => {
      const path = new URL(url, 'http://test').pathname
      if (url === '/api/auth/me') return Response.json(me)
      if (path === '/api/customers/c1/erasure') {
        if (erasureAnswer) return erasureAnswer()
        const held = (current.retention as { status: string }).status !== 'not_held'
        const erasure = held
          ? ERASED_HELD
          : {
              ...ERASED_HELD,
              held: false,
              held_until: null,
              held_reason: null,
              retained: [],
              purged_at: '2026-09-21T15:00:01Z',
            }
        current = held
          ? { ...current, email: null, phone: null, notes: null, suppressed: true, erasure }
          : {
              ...current,
              first_name: 'Erased',
              last_name: 'Client',
              date_of_birth: null,
              email: null,
              phone: null,
              notes: null,
              suppressed: true,
              erasure,
            }
        return Response.json(erasure, { status: 201 })
      }
      if (path === '/api/customers/c1') {
        return Response.json({ customer: current, timezone: 'America/Toronto', appointments })
      }
      return undefined
    },
  })
}

const erasureCalls = (calls: Call[]) => calls.filter((c) => c.url.endsWith('/erasure'))

async function openDialog() {
  const user = userEvent.setup()
  renderApp('/clients/c1')
  // By keyboard: after one render in a module, a Radix `DropdownMenu` stops answering a fresh
  // `userEvent.setup()`'s pointerdown (see `schedule-lifecycle-complete.test.tsx`). Enter on
  // the focused trigger is also the path a keyboard user takes.
  ;(await screen.findByRole('button', { name: 'More actions' })).focus()
  await user.keyboard('{Enter}')
  await user.click(await screen.findByRole('menuitem', { name: 'Request erasure' }))
  return { user, dialog: await screen.findByRole('dialog', { name: 'Erase this client?' }) }
}

test('with no hold, the dialog says names and DOB go too, and nothing is sent until confirmed', async () => {
  const { calls } = fake()
  const { user, dialog } = await openDialog()

  expect(within(dialog).getByText(/Not under a retention hold/)).toBeInTheDocument()
  const removed = within(dialog).getByRole('list', { name: 'Removed now' })
  for (const item of [/Name/, /Date of birth/, /Email and phone/, /contacts/, /Notes/, /document key/]) {
    expect(within(removed).getByText(item)).toBeInTheDocument()
  }
  const kept = within(dialog).getByRole('list', { name: 'Kept' })
  expect(within(kept).getByText(/Visit history/)).toBeInTheDocument()
  expect(within(kept).queryByText(/Date of birth/)).not.toBeInTheDocument()
  expect(erasureCalls(calls)).toHaveLength(0)

  await user.type(within(dialog).getByLabelText('Note (optional)'), 'Asked by phone')
  await user.click(within(dialog).getByRole('button', { name: 'Erase client' }))

  await waitFor(() => expect(erasureCalls(calls)).toHaveLength(1))
  expect(erasureCalls(calls)[0].body).toEqual({ note: 'Asked by phone' })
  const banner = await screen.findByRole('status', { name: 'Erasure requested' })
  expect(banner).toHaveTextContent(/Nothing personal is retained/)
  expect(await screen.findByRole('heading', { name: 'Erased Client' })).toBeInTheDocument()
})

test('under a hold, the dialog says what is kept, why and until when', async () => {
  fake({ customer: { ...DETAIL, retention: HELD } })
  const { dialog } = await openDialog()

  expect(within(dialog).getByText(/regulated health record/i)).toHaveTextContent(/2042/)
  const removed = within(dialog).getByRole('list', { name: 'Removed now' })
  expect(within(removed).queryByText(/^Name/)).not.toBeInTheDocument()
  expect(within(removed).getByText(/Email and phone/)).toBeInTheDocument()
  const kept = within(dialog).getByRole('list', { name: 'Kept' })
  for (const item of [/Name/, /Date of birth/, /Visit history/]) {
    expect(within(kept).getByText(item)).toBeInTheDocument()
  }
})

test('a hold that has already ended is not promised: name and DOB go, and the dialog says why', async () => {
  fake({ customer: { ...DETAIL, retention: { status: 'expired', expires_on: '2025-08-01' } } })
  const { dialog } = await openDialog()

  expect(within(dialog).getByText(/Retention period ended .*2025 — no longer held/)).toBeInTheDocument()
  const removed = within(dialog).getByRole('list', { name: 'Removed now' })
  expect(within(removed).getByText(/^Name/)).toBeInTheDocument()
  expect(within(removed).getByText(/Date of birth/)).toBeInTheDocument()
  const kept = within(dialog).getByRole('list', { name: 'Kept' })
  expect(within(kept).queryByText(/Date of birth/)).not.toBeInTheDocument()
})

test('a chart held with no date of birth says it is held until one is recorded', async () => {
  fake({ customer: { ...DETAIL, date_of_birth: null, retention: { status: 'needs_dob', expires_on: null } } })
  const { dialog } = await openDialog()

  expect(within(dialog).getByText(/until a date of birth is recorded/)).toBeInTheDocument()
})

test('an erased, held profile shows the banner with what is kept and the date', async () => {
  fake({
    customer: {
      ...DETAIL,
      email: null,
      phone: null,
      notes: null,
      retention: HELD,
      suppressed: true,
      erasure: ERASED_HELD,
    },
  })
  renderApp('/clients/c1')

  const banner = await screen.findByRole('status', { name: 'Erasure requested' })
  expect(banner).toHaveTextContent('Retained: Name, Date of birth, Visit history')
  expect(banner).toHaveTextContent('retained until 14 Mar 2042, then destroyed')
  expect(banner).toHaveTextContent(/hidden from the client list/)
  // Already erased: nothing to ask for again.
  expect(screen.queryByRole('button', { name: 'More actions' })).not.toBeInTheDocument()
})

test('outside Admin Mode, or without the capability, there is no erasure action', async () => {
  fake({ me: STAFF_MODE })
  const { unmount } = renderApp('/clients/c1')
  await screen.findByRole('heading', { name: 'Priya Nair' })
  expect(screen.queryByRole('button', { name: 'More actions' })).not.toBeInTheDocument()
  unmount()
  vi.unstubAllGlobals()

  fake({ me: { ...ME, capabilities: ['admin', 'customers.view', 'customers.manage'] } })
  renderApp('/clients/c1')
  await screen.findByRole('heading', { name: 'Priya Nair' })
  expect(screen.queryByRole('button', { name: 'More actions' })).not.toBeInTheDocument()
})

test('an erasure refreshes the calendar too, whose cards carry the client name and contacts', async () => {
  const { calls } = fake()
  const { user, dialog } = await openDialog()
  const invalidate = vi.spyOn(QueryClient.prototype, 'invalidateQueries')

  await user.click(within(dialog).getByRole('button', { name: 'Erase client' }))

  await waitFor(() => expect(erasureCalls(calls)).toHaveLength(1))
  // The calendar's one read is `['schedule', …]`; `['appointments']` backs no query.
  await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['schedule'] }))
})

test('an erased, held client offers only a date-of-birth correction, and sends only that', async () => {
  const { calls } = fake({
    customer: {
      ...DETAIL,
      email: null,
      phone: null,
      notes: null,
      retention: HELD,
      suppressed: true,
      erasure: ERASED_HELD,
    },
  })
  const user = userEvent.setup()
  renderApp('/clients/c1')

  await user.click(await screen.findByRole('button', { name: 'Edit details' }))
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByText(/only the date of birth can be corrected/i)).toBeInTheDocument()
  expect(within(dialog).queryAllByRole('textbox')).toHaveLength(0)
  expect(within(dialog).queryByLabelText('Email')).not.toBeInTheDocument()
  const dob = within(dialog).getByLabelText('Date of birth')
  fireEvent.change(dob, { target: { value: '2014-05-01' } })
  await user.click(within(dialog).getByRole('button', { name: 'Save changes' }))

  await waitFor(() =>
    expect(calls.filter((c) => c.method === 'PATCH').map((c) => c.body)).toEqual([
      { date_of_birth: '2014-05-01' },
    ]),
  )
})

test('not held and the purge not yet run, the banner says what is still to go', async () => {
  fake({
    customer: {
      ...DETAIL,
      retention: { status: 'expired', expires_on: '2026-09-01' },
      suppressed: true,
      erasure: { ...ERASED_HELD, held: false, held_until: null, held_reason: null, retained: [] },
    },
  })
  renderApp('/clients/c1')

  const banner = await screen.findByRole('status', { name: 'Erasure requested' })
  expect(banner).not.toHaveTextContent(/Nothing personal is retained/)
  expect(banner).toHaveTextContent(/Not under a retention hold/)
  expect(banner).not.toHaveTextContent(/No longer held/) // wrong for a client never held
  expect(banner).toHaveTextContent(/still on this record is removed by tonight/)
})

const UPCOMING_BLOCK = /This client has 2 upcoming appointment\(s\)\. Cancel or complete them before requesting erasure\./

test('upcoming confirmed visits block the request: the dialog says so and offers no erase button', async () => {
  const { calls } = fake({
    appointments: [
      visit('a1', 3),
      visit('a2', 10),
      visit('a3', 5, 'cancelled'),
      visit('a4', -2), // past, never closed out: cannot happen after an erasure, so no block
      visit('a5', -9, 'completed'),
    ],
  })
  const { dialog } = await openDialog()

  expect(within(dialog).getByText(UPCOMING_BLOCK)).toBeInTheDocument()
  expect(within(dialog).queryByRole('button', { name: 'Erase client' })).not.toBeInTheDocument()
  expect(erasureCalls(calls)).toHaveLength(0)
})

test("a 409 upcoming_appointments from the server shows the same message with the server's count", async () => {
  fake({
    erasureAnswer: () =>
      Response.json(
        {
          detail: 'This client has 2 upcoming appointment(s).',
          code: 'upcoming_appointments',
          count: 2,
        },
        { status: 409 },
      ),
  })
  const { user, dialog } = await openDialog()

  await user.click(within(dialog).getByRole('button', { name: 'Erase client' }))

  expect(await within(dialog).findByText(UPCOMING_BLOCK)).toBeInTheDocument()
  expect(within(dialog).queryByRole('button', { name: 'Erase client' })).not.toBeInTheDocument()
})

test('any appointment change marks every cached client profile stale, so a blocked erasure clears', async () => {
  // The dialog counts upcoming visits from the profile; a cancel on the calendar must reach it.
  const client = new QueryClient()
  client.setQueryData(['customer-profile', 'c1'], { appointments: [] })

  invalidateScheduling(client)

  expect(client.getQueryState(['customer-profile', 'c1'])?.isInvalidated).toBe(true)
})
