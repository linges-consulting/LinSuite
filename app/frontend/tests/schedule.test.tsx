import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { toast } from 'sonner'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { Toaster } from '@/components/ui/sonner'
import { createQueryClient } from '@/lib/query-client'
import { ThemeProvider } from '@/lib/theme'
import { SchedulePage } from '@/routes/schedule'

/**
 * The schedule and its booking dialog, against a small fake of the four endpoints they read
 * and the one they write. What is worth pinning: that the dialog sends back the *server's*
 * slot instant rather than one it computed, that an inline client goes as a `customer`
 * body, that a lost race is told as "just taken" and the slots are read again, and that
 * the day's appointments land in the right columns.
 */

const ROSTER = [
  { id: 's1', display_name: 'Ana Rossi', colour: 'blue', hex: '#1d4ed8', dark_hex: '#659dff', sort_order: 0 },
  { id: 's2', display_name: 'Bo Chen', colour: 'teal', hex: '#0f766e', dark_hex: '#68b5ac', sort_order: 1 },
]

const CATALOG = [
  {
    id: 'v1',
    name: 'Swedish Massage',
    description: null,
    duration_minutes: 60,
    buffer_before_minutes: 0,
    buffer_after_minutes: 15,
    price_cents: 12000,
    bookable_online: true,
    sort_order: 0,
    requirements: [{ kind: 'space', resource_id: null, resource_name: null, resource_active: null }],
    staff_ids: ['s1', 's2'],
    bookable: true,
    unbookable_reasons: [],
  },
]

// 10:00 and 10:15 Toronto on a summer day, as the server sends them.
const TEN = '2026-06-15T14:00:00Z'
const TEN_FIFTEEN = '2026-06-15T14:15:00Z'
const SLOTS = [
  { starts_at: TEN, ends_at: '2026-06-15T15:00:00Z', staff_ids: ['s1', 's2'] },
  { starts_at: TEN_FIFTEEN, ends_at: '2026-06-15T15:15:00Z', staff_ids: ['s2'] },
]

const PRIYA = { id: 'c1', first_name: 'Priya', last_name: 'Nair', email: null, phone: '4165550199' }

const BOOKED = {
  id: 'a1',
  status: 'confirmed',
  starts_at: TEN,
  ends_at: '2026-06-15T15:00:00Z',
  duration_minutes: 60,
  buffer_before_minutes: 0,
  buffer_after_minutes: 15,
  price_cents: 12000,
  notes: null,
  booking_group_id: null,
  customer: PRIYA,
  service: { id: 'v1', name: 'Swedish Massage' },
  staff: { id: 's2', display_name: 'Bo Chen', colour: 'teal' },
  resources: [{ id: 'r1', name: 'Room 1', kind: 'space' }],
}

type Call = { url: string; method: string; body?: any }

function fakeServer({
  firstBookingTaken = false,
  firstBookingGone = false,
  appointments = [] as any[],
  timezone = 'America/Toronto',
} = {}) {
  const calls: Call[] = []
  let bookings = 0
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      const body = init?.body ? JSON.parse(init.body as string) : undefined
      calls.push({ url, method, body })
      if (url === '/api/branding') {
        return Response.json({
          name: 'Cedar Lane Clinic',
          timezone,
          colors: {},
          logo_url: null,
          logo_etag: null,
          favicon_url: null,
          favicon_etag: null,
        })
      }
      if (url === '/api/staff') return Response.json({ staff: ROSTER })
      if (url === '/api/catalog/services') return Response.json({ services: CATALOG })
      if (url.startsWith('/api/appointments?')) {
        return Response.json({ timezone: 'America/Toronto', appointments })
      }
      if (url.startsWith('/api/availability?')) {
        const params = new URLSearchParams(url.split('?')[1])
        const staff = params.get('staff_id')
        return Response.json({
          service_id: 'v1',
          timezone: 'America/Toronto',
          granularity_minutes: 15,
          horizon_ends_on: '2026-09-13',
          days: [
            {
              date: params.get('from'),
              slots: staff ? SLOTS.filter((s) => s.staff_ids.includes(staff)) : SLOTS,
            },
          ],
        })
      }
      if (url.startsWith('/api/customers?')) {
        const q = new URLSearchParams(url.split('?')[1]).get('q') ?? ''
        return Response.json({ customers: 'priya'.startsWith(q.toLowerCase()) ? [PRIYA] : [] })
      }
      if (url === '/api/appointments' && method === 'POST') {
        bookings += 1
        if (firstBookingTaken && bookings === 1) {
          return Response.json(
            { detail: 'That time was just taken. Pick another.', code: 'slot_taken' },
            { status: 409 },
          )
        }
        if (firstBookingGone && bookings === 1) {
          // The engine's own refusal: somebody booked it just *before* the race window.
          return Response.json(
            {
              detail: [
                {
                  type: 'value_error',
                  loc: ['body', 'starts_at'],
                  msg: 'That time is no longer available. Pick another.',
                },
              ],
              code: 'not_offered',
            },
            { status: 422 },
          )
        }
        return Response.json(BOOKED, { status: 201 })
      }
      return Response.json({}, { status: 404 })
    }),
  )
  return calls
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

/** Through the dialog as far as a chosen provider and a chosen slot. */
async function pickServiceProviderAndSlot(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: 'New appointment' }))
  await user.click(await screen.findByRole('combobox', { name: 'Service' }))
  await user.click(await screen.findByRole('option', { name: /Swedish Massage/ }))
  await user.click(screen.getByRole('combobox', { name: 'Provider' }))
  await user.click(await screen.findByRole('option', { name: 'Bo Chen' }))
  // Bo's own slots: both. The server sent instants; the buttons print them in Toronto.
  await user.click(await screen.findByRole('button', { name: '10:15 AM' }))
}

beforeEach(() => {
  vi.unstubAllGlobals()
  toast.dismiss()
})

afterEach(() => vi.useRealTimers())

test('the page opens on the business\'s today, whatever the browser\'s clock says', async () => {
  // 22:00 UTC on the 15th: still the 15th anywhere west of UTC+2, already the 16th on
  // Kiritimati (UTC+14) — the business's calendar wins.
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-06-15T22:00:00Z'))
  fakeServer({ timezone: 'Pacific/Kiritimati' })
  const user = userEvent.setup()
  renderSchedule()

  expect(await screen.findByLabelText('Day')).toHaveValue('2026-06-16')
  await user.click(screen.getByRole('button', { name: 'Next day' }))
  expect(screen.getByLabelText('Day')).toHaveValue('2026-06-17')
  await user.click(screen.getByRole('button', { name: 'Today' }))
  expect(screen.getByLabelText('Day')).toHaveValue('2026-06-16')
})

test('the columns scroll inside their own box, never the page', async () => {
  fakeServer()
  renderSchedule()

  await screen.findByRole('region', { name: 'Bo Chen' })
  expect(screen.getByTestId('columns')).toHaveClass('overflow-x-auto')
})

test('the day lists each appointment under its staff column', async () => {
  fakeServer({ appointments: [BOOKED] })

  renderSchedule()

  const bo = await screen.findByRole('region', { name: 'Bo Chen' })
  expect(within(bo).getByText('Priya Nair')).toBeInTheDocument()
  expect(within(bo).getByText('Swedish Massage · Room 1')).toBeInTheDocument()
  expect(within(bo).getByText('10:00 AM–11:00 AM')).toBeInTheDocument()
  const ana = screen.getByRole('region', { name: 'Ana Rossi' })
  expect(within(ana).getByText('Nothing booked.')).toBeInTheDocument()
})

test('picking a slot sends its own starts_at back, with an existing client', async () => {
  const calls = fakeServer()
  const user = userEvent.setup()
  renderSchedule()

  await pickServiceProviderAndSlot(user)
  await user.type(screen.getByRole('textbox', { name: 'Find a client' }), 'pri')
  await user.click(await screen.findByRole('button', { name: /Priya Nair/ }))
  await user.click(screen.getByRole('button', { name: 'Book' }))

  await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
  const posted = calls.find((c) => c.method === 'POST')!
  expect(posted.url).toBe('/api/appointments')
  expect(posted.body).toEqual({
    service_id: 'v1',
    staff_id: 's2',
    starts_at: TEN_FIFTEEN,
    customer_id: 'c1',
    notes: null,
  })
  // The slots were asked for the chosen provider, not the union.
  expect(calls.some((c) => c.url.includes('/api/availability?') && c.url.includes('staff_id=s2'))).toBe(true)
  expect(await screen.findByText('Booked Priya Nair with Bo Chen in Room 1')).toBeInTheDocument()
  // The day is read again so the new appointment shows up in its column.
  await waitFor(() =>
    expect(calls.filter((c) => c.url.startsWith('/api/appointments?')).length).toBeGreaterThan(1),
  )
})

test('"any available" sends no staff id and an inline client goes as a customer body', async () => {
  const calls = fakeServer()
  const user = userEvent.setup()
  renderSchedule()

  await user.click(await screen.findByRole('button', { name: 'New appointment' }))
  await user.click(await screen.findByRole('combobox', { name: 'Service' }))
  await user.click(await screen.findByRole('option', { name: /Swedish Massage/ }))
  // An "Any" row with every start once, then each person's own: 10:00 under both names,
  // 10:15 under Bo only.
  const any = await screen.findByRole('group', { name: 'Any available' })
  expect(within(any).getAllByRole('button')).toHaveLength(2)
  expect(within(screen.getByRole('group', { name: 'Ana Rossi' })).getAllByRole('button')).toHaveLength(1)
  expect(within(screen.getByRole('group', { name: 'Bo Chen' })).getAllByRole('button')).toHaveLength(2)
  await user.click(within(any).getByRole('button', { name: '10:00 AM' }))
  await user.click(screen.getByRole('button', { name: 'New client' }))
  await user.type(screen.getByLabelText('First name'), 'Sam')
  await user.type(screen.getByLabelText('Last name'), 'Okonkwo')
  await user.type(screen.getByLabelText('Phone'), '647-555-0100')
  await user.type(screen.getByLabelText('Notes'), 'Prefers firm pressure')
  await user.click(screen.getByRole('button', { name: 'Book' }))

  await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
  expect(calls.find((c) => c.method === 'POST')!.body).toEqual({
    service_id: 'v1',
    staff_id: null,
    starts_at: TEN,
    customer: { first_name: 'Sam', last_name: 'Okonkwo', email: null, phone: '647-555-0100' },
    notes: 'Prefers firm pressure',
  })
})

test('a slot picked under a person\'s name books that person', async () => {
  const calls = fakeServer()
  const user = userEvent.setup()
  renderSchedule()

  await user.click(await screen.findByRole('button', { name: 'New appointment' }))
  await user.click(await screen.findByRole('combobox', { name: 'Service' }))
  await user.click(await screen.findByRole('option', { name: /Swedish Massage/ }))
  const bo = await screen.findByRole('group', { name: 'Bo Chen' })
  await user.click(within(bo).getByRole('button', { name: '10:00 AM' }))

  // The provider is now Bo, the pick survives, and the slots are his own list.
  expect(screen.getByRole('combobox', { name: 'Provider' })).toHaveTextContent('Bo Chen')
  await waitFor(() =>
    expect(screen.getByRole('button', { name: '10:00 AM' })).toHaveAttribute('aria-pressed', 'true'),
  )
  await user.type(screen.getByRole('textbox', { name: 'Find a client' }), 'pri')
  await user.click(await screen.findByRole('button', { name: /Priya Nair/ }))
  // The chosen client's number reads as a person writes it, clear of the name.
  expect(screen.getByText('(416) 555-0199')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Book' }))

  await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
  expect(calls.find((c) => c.method === 'POST')!.body).toMatchObject({
    staff_id: 's2',
    starts_at: TEN,
    customer_id: 'c1',
  })
})

test('a slot taken in the meantime is said so, the pick is dropped and the slots re-read', async () => {
  const calls = fakeServer({ firstBookingTaken: true })
  const user = userEvent.setup()
  renderSchedule()

  await pickServiceProviderAndSlot(user)
  const before = calls.filter((c) => c.url.startsWith('/api/availability?')).length
  await user.type(screen.getByRole('textbox', { name: 'Find a client' }), 'pri')
  await user.click(await screen.findByRole('button', { name: /Priya Nair/ }))
  await user.click(screen.getByRole('button', { name: 'Book' }))

  expect(await screen.findByText('That time was just taken. Pick another.')).toBeInTheDocument()
  // Still open, nothing picked, and the day has been asked for again.
  expect(screen.getByRole('dialog', { name: 'New appointment' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '10:15 AM' })).toHaveAttribute('aria-pressed', 'false')
  expect(screen.getByRole('button', { name: 'Book' })).toBeDisabled()
  await waitFor(() =>
    expect(calls.filter((c) => c.url.startsWith('/api/availability?')).length).toBeGreaterThan(before),
  )

  // Picking again and booking again goes through.
  await user.click(screen.getByRole('button', { name: '10:00 AM' }))
  await user.click(screen.getByRole('button', { name: 'Book' }))
  expect(await screen.findByText('Booked Priya Nair with Bo Chen in Room 1')).toBeInTheDocument()
})

test('a slot the engine no longer offers is treated the same way as a lost race', async () => {
  const calls = fakeServer({ firstBookingGone: true })
  const user = userEvent.setup()
  renderSchedule()

  await pickServiceProviderAndSlot(user)
  const before = calls.filter((c) => c.url.startsWith('/api/availability?')).length
  await user.type(screen.getByRole('textbox', { name: 'Find a client' }), 'pri')
  await user.click(await screen.findByRole('button', { name: /Priya Nair/ }))
  await user.click(screen.getByRole('button', { name: 'Book' }))

  expect(await screen.findByText('That time is no longer available. Pick another.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '10:15 AM' })).toHaveAttribute('aria-pressed', 'false')
  expect(screen.getByRole('button', { name: 'Book' })).toBeDisabled()
  await waitFor(() =>
    expect(calls.filter((c) => c.url.startsWith('/api/availability?')).length).toBeGreaterThan(before),
  )
})
