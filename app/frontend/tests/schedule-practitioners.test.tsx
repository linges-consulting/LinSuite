import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { Toaster } from '@/components/ui/sonner'
import { createQueryClient } from '@/lib/query-client'
import { ThemeProvider } from '@/lib/theme'
import { SchedulePage } from '@/routes/schedule'

/**
 * The calendar's "Practitioners" picker (issue: "In the calendar it currently shows all
 * practitioners…"): a signed-in practitioner defaults to their own column, anybody else
 * defaults to every active practitioner, the toolbar control changes which columns show,
 * and the choice is remembered per account in `localStorage` — falling back cleanly to the
 * default whenever the stored pick is empty, invalid, or unreadable at all (private mode).
 *
 * A small roster of its own rather than `tests/schedule.test.tsx`'s shared fixture: that
 * file's signed-in account and both staff members are practitioners with both columns
 * pre-selected (its own `seedBothPractitionersChosen`), on purpose, so its drag/booking
 * tests are not also, incidentally, tests of the picker's default. This file is the
 * opposite: every test here is about the default and the picker.
 */

type Roster = {
  id: string
  display_name: string
  colour: string
  hex: string
  dark_hex: string
  sort_order: number
  user_id: string
  is_practitioner: boolean
  max_concurrent_appointments: number
}

const ANA: Roster = {
  id: 's1',
  display_name: 'Ana Rossi',
  colour: 'blue',
  hex: '#1d4ed8',
  dark_hex: '#659dff',
  sort_order: 0,
  user_id: 'u1',
  is_practitioner: true,
  max_concurrent_appointments: 1,
}
const BO: Roster = {
  id: 's2',
  display_name: 'Bo Chen',
  colour: 'teal',
  hex: '#0f766e',
  dark_hex: '#68b5ac',
  sort_order: 1,
  user_id: 'u2',
  is_practitioner: true,
  max_concurrent_appointments: 2,
}
/** Front desk: a staff row (an account, like everyone else) that delivers no services. */
const FRONT_DESK: Roster = {
  id: 's0',
  display_name: 'Riley Desk',
  colour: 'violet',
  hex: '#6d28d9',
  dark_hex: '#a78bfa',
  sort_order: 0,
  user_id: 'u1',
  is_practitioner: false,
  max_concurrent_appointments: 1,
}

const TEN = '2026-06-15T14:00:00Z'
const ELEVEN = '2026-06-15T15:00:00Z'
const PRIYA = { id: 'c1', first_name: 'Priya', last_name: 'Nair', email: null, phone: null, classification: 'new' as const }

function appointmentFor(staff: Roster, id: string) {
  return {
    id,
    status: 'confirmed',
    starts_at: TEN,
    ends_at: ELEVEN,
    duration_minutes: 60,
    buffer_before_minutes: 0,
    buffer_after_minutes: 0,
    price_cents: 10000,
    notes: null,
    booking_group_id: null,
    overridden_rules: null,
    override_reason: null,
    customer: PRIYA,
    service: { id: 'v1', name: 'Swedish Massage' },
    staff: { id: staff.id, display_name: staff.display_name, colour: staff.colour },
    resources: [],
  }
}

function fakeServer({
  userId = 'u1',
  roster,
  appointments = [] as ReturnType<typeof appointmentFor>[],
}: {
  userId?: string
  roster: Roster[]
  appointments?: ReturnType<typeof appointmentFor>[]
}) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url === '/api/branding') {
        return Response.json({
          name: 'Cedar Lane Clinic',
          timezone: 'America/Toronto',
          colors: {},
          logo_url: null,
          logo_etag: null,
          favicon_url: null,
          favicon_etag: null,
        })
      }
      if (url === '/api/auth/me') {
        return Response.json({
          id: userId,
          email: 'person@cedar.example',
          role: 'Staff',
          capabilities: ['schedule.view'],
          mode: 'staff',
          can_switch_modes: false,
          admin_grant_expires_at: null,
          admin_hard_limit_at: null,
          must_change_password: false,
          mfa: { enrolled: false, method: null, pending: false, enrolment_required: false, verified_at: null, email_otp_allowed: false },
        })
      }
      if (url === '/api/staff') return Response.json({ staff: roster })
      if (url.startsWith('/api/schedule?')) {
        const params = new URLSearchParams(url.split('?')[1])
        const staffId = params.get('staff_id')
        const visible = staffId ? roster.filter((s) => s.id === staffId) : roster
        return Response.json({
          timezone: 'America/Toronto',
          granularity_minutes: 15,
          staff: visible.map((s) => ({
            id: s.id,
            display_name: s.display_name,
            colour: s.colour,
            hex: s.hex,
            dark_hex: s.dark_hex,
            max_concurrent_appointments: s.max_concurrent_appointments,
          })),
          working_blocks: [],
          time_off: [],
          closures: [],
          appointments: appointments.filter((a) => !staffId || a.staff.id === staffId),
        })
      }
      return Response.json({}, { status: 404 })
    }),
  )
}

function renderSchedule() {
  return render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter>
          <SchedulePage />
        </MemoryRouter>
        <Toaster position="bottom-right" />
      </QueryClientProvider>
    </ThemeProvider>,
  )
}

function onTheFifteenth() {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-06-15T14:32:00Z'))
}

beforeEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})
afterEach(() => vi.useRealTimers())

test("a practitioner's default is their own column", async () => {
  onTheFifteenth()
  fakeServer({ roster: [ANA, BO], appointments: [appointmentFor(ANA, 'a1'), appointmentFor(BO, 'a2')] })
  renderSchedule()

  expect(await screen.findByRole('region', { name: 'Ana Rossi' })).toBeInTheDocument()
  expect(screen.queryByRole('region', { name: 'Bo Chen' })).not.toBeInTheDocument()
  // Efficient: the server did the narrowing, not a client-side filter over everyone's day.
  expect(screen.getByRole('button', { name: /Practitioners/ })).toHaveTextContent('Only me')
})

test("a non-practitioner's default is everyone", async () => {
  onTheFifteenth()
  fakeServer({
    roster: [FRONT_DESK, ANA, BO],
    appointments: [appointmentFor(ANA, 'a1'), appointmentFor(BO, 'a2')],
  })
  renderSchedule()

  expect(await screen.findByRole('region', { name: 'Ana Rossi' })).toBeInTheDocument()
  expect(screen.getByRole('region', { name: 'Bo Chen' })).toBeInTheDocument()
  // Front desk is not a practitioner and never gets a column of its own by default.
  expect(screen.queryByRole('region', { name: 'Riley Desk' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: /Practitioners/ })).toHaveTextContent('All')
})

test('the picker changes the columns shown', async () => {
  onTheFifteenth()
  fakeServer({
    userId: 'u3',
    roster: [ANA, BO],
    appointments: [appointmentFor(ANA, 'a1'), appointmentFor(BO, 'a2')],
  })
  const user = userEvent.setup()
  renderSchedule()

  await screen.findByRole('region', { name: 'Ana Rossi' })
  await screen.findByRole('region', { name: 'Bo Chen' })

  await user.click(screen.getByRole('button', { name: /Practitioners/ }))
  await user.click(screen.getByRole('checkbox', { name: /Bo Chen/ }))

  await waitFor(() => expect(screen.queryByRole('region', { name: 'Bo Chen' })).not.toBeInTheDocument())
  expect(screen.getByRole('region', { name: 'Ana Rossi' })).toBeInTheDocument()
})

test('the selection persists across remounts, using localStorage', async () => {
  onTheFifteenth()
  fakeServer({
    userId: 'u3',
    roster: [ANA, BO],
    appointments: [appointmentFor(ANA, 'a1'), appointmentFor(BO, 'a2')],
  })
  const user = userEvent.setup()
  const first = renderSchedule()

  await screen.findByRole('region', { name: 'Ana Rossi' })
  await user.click(screen.getByRole('button', { name: /Practitioners/ }))
  await user.click(screen.getByRole('checkbox', { name: /Ana Rossi/ }))
  await waitFor(() => expect(screen.queryByRole('region', { name: 'Ana Rossi' })).not.toBeInTheDocument())
  first.unmount()

  expect(JSON.parse(localStorage.getItem('linsuite.schedule.practitioners.u3')!)).toEqual(['s2'])

  renderSchedule()
  expect(await screen.findByRole('region', { name: 'Bo Chen' })).toBeInTheDocument()
  expect(screen.queryByRole('region', { name: 'Ana Rossi' })).not.toBeInTheDocument()
})

test('a deactivated practitioner drops out, and the rest of the pick survives', async () => {
  onTheFifteenth()
  localStorage.setItem('linsuite.schedule.practitioners.u3', JSON.stringify(['s1', 'no-longer-staff']))
  fakeServer({ userId: 'u3', roster: [ANA, BO], appointments: [appointmentFor(ANA, 'a1'), appointmentFor(BO, 'a2')] })
  renderSchedule()

  // The stored, now-nonexistent id is silently dropped; the still-valid one is kept, and the
  // pick does not widen back out to "everyone" just because part of it was stale.
  expect(await screen.findByRole('region', { name: 'Ana Rossi' })).toBeInTheDocument()
  expect(screen.queryByRole('region', { name: 'Bo Chen' })).not.toBeInTheDocument()
})

/** Only this feature's own key throws — everything else (the theme, in particular, which
 *  reads `localStorage` unconditionally on mount) keeps working, the way a real "this one
 *  site's storage is blocked" failure would. */
const PRACTITIONERS_KEY = /^linsuite\.schedule\.practitioners\./

test('storage throwing falls back cleanly to the default, and the picker still works', async () => {
  onTheFifteenth()
  const realGetItem = Storage.prototype.getItem
  const realSetItem = Storage.prototype.setItem
  const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(function (this: Storage, key) {
    if (PRACTITIONERS_KEY.test(key)) throw new DOMException('blocked', 'SecurityError')
    return realGetItem.call(this, key)
  })
  fakeServer({ roster: [ANA, BO], appointments: [appointmentFor(ANA, 'a1'), appointmentFor(BO, 'a2')] })
  const user = userEvent.setup()
  renderSchedule()

  // Falls back to the practitioner default (own column) rather than throwing through render.
  expect(await screen.findByRole('region', { name: 'Ana Rossi' })).toBeInTheDocument()
  expect(screen.queryByRole('region', { name: 'Bo Chen' })).not.toBeInTheDocument()

  const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
    if (PRACTITIONERS_KEY.test(key)) throw new DOMException('blocked', 'SecurityError')
    return realSetItem.call(this, key, value)
  })
  await user.click(screen.getByRole('button', { name: /Practitioners/ }))
  await user.click(screen.getByRole('checkbox', { name: /Bo Chen/ }))
  // The write fails too, but the pick still takes effect for this render.
  await waitFor(() => expect(screen.getByRole('region', { name: 'Bo Chen' })).toBeInTheDocument())

  getItem.mockRestore()
  setItem.mockRestore()
})
