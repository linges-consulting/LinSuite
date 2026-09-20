import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { toast } from 'sonner'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { Toaster } from '@/components/ui/sonner'
import { createQueryClient } from '@/lib/query-client'
import { ThemeProvider } from '@/lib/theme'
import { SchedulePage } from '@/routes/schedule'

/**
 * The schedule grid and its booking dialog, against a small fake of the endpoints they read
 * and the two they write. What is worth pinning on the grid: appointments land in the right
 * column at the right pixel, two at once sit side by side, drawing on empty space opens the
 * dialog prefilled, the keyboard moves and resizes and Escape puts back, a refused move
 * rolls back with the reason on screen, all-day items sit in their row, and the week view
 * asks for seven days. On the dialog: that it sends back the *server's* slot instant, that
 * an inline client goes as a `customer` body, and that a lost race is told and re-read.
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
const DAY = '2026-06-15'
const TEN = '2026-06-15T14:00:00Z'
const TEN_FIFTEEN = '2026-06-15T14:15:00Z'
const ELEVEN = '2026-06-15T15:00:00Z'
const SLOTS = [
  { starts_at: TEN, ends_at: ELEVEN, staff_ids: ['s1', 's2'] },
  { starts_at: TEN_FIFTEEN, ends_at: '2026-06-15T15:15:00Z', staff_ids: ['s2'] },
]

const PRIYA = { id: 'c1', first_name: 'Priya', last_name: 'Nair', email: null, phone: '4165550199' }

const BOOKED = {
  id: 'a1',
  status: 'confirmed',
  starts_at: TEN,
  ends_at: ELEVEN,
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

const OVERLAPPING = {
  ...BOOKED,
  id: 'a2',
  starts_at: '2026-06-15T14:30:00Z',
  ends_at: '2026-06-15T15:30:00Z',
  customer: { ...PRIYA, id: 'c2', first_name: 'Sam', last_name: 'Okonkwo' },
}

const SHORT = {
  ...BOOKED,
  id: 'a3',
  starts_at: '2026-06-15T18:00:00Z',
  ends_at: '2026-06-15T18:15:00Z',
  duration_minutes: 15,
  staff: { id: 's1', display_name: 'Ana Rossi', colour: 'blue' },
  customer: { ...PRIYA, id: 'c3', first_name: 'Quick', last_name: 'Trim' },
}

type Call = { url: string; method: string; body?: any }

function fakeServer({
  firstBookingTaken = false,
  firstBookingGone = false,
  moveRefused = null as null | 'not_offered' | 'slot_taken',
  appointments = [] as any[],
  timeOff = [] as any[],
  closures = [] as any[],
  timezone = 'America/Toronto',
} = {}) {
  const calls: Call[] = []
  let bookings = 0
  // Copies: a PATCH changes the fake's rows, and the next test wants the originals.
  appointments = appointments.map((a) => ({ ...a }))
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
      if (url === '/api/auth/me') {
        return Response.json({
          id: 'u1',
          email: 'owner@cedar.example',
          role: 'Staff',
          capabilities: ['schedule.view', 'schedule.manage', 'customers.manage'],
          mode: 'staff',
          can_switch_modes: false,
          admin_grant_expires_at: null,
          admin_hard_limit_at: null,
          must_change_password: false,
          mfa: { enrolled: false, method: null, pending: false, enrolment_required: false, verified_at: null, email_otp_allowed: false },
        })
      }
      if (url === '/api/staff') return Response.json({ staff: ROSTER })
      if (url === '/api/catalog/services') return Response.json({ services: CATALOG })
      if (url.startsWith('/api/schedule?')) {
        const params = new URLSearchParams(url.split('?')[1])
        const staff = params.get('staff_id')
        const from = params.get('from')!
        const dates = [from]
        while (dates[dates.length - 1] < params.get('to')!) {
          const [y, m, d] = dates[dates.length - 1].split('-').map(Number)
          dates.push(new Date(Date.UTC(y, m - 1, d + 1)).toISOString().slice(0, 10))
        }
        return Response.json({
          timezone: 'America/Toronto',
          granularity_minutes: 15,
          staff: ROSTER.filter((s) => !staff || s.id === staff).map((s) => ({
            id: s.id,
            display_name: s.display_name,
            colour: s.colour,
            hex: s.hex,
            dark_hex: s.dark_hex,
          })),
          // Everybody works 09:00–17:00 Toronto on every asked-for day.
          working_blocks: dates.flatMap((date) =>
            ROSTER.filter((s) => !staff || s.id === staff).map((s) => ({
              staff_id: s.id,
              date,
              starts_at: `${date}T13:00:00Z`,
              ends_at: `${date}T21:00:00Z`,
            })),
          ),
          time_off: timeOff,
          closures,
          appointments: appointments.filter((a) => !staff || a.staff.id === staff),
        })
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
      if (url.startsWith('/api/appointments/') && method === 'PATCH') {
        if (moveRefused === 'slot_taken') {
          return Response.json(
            { detail: 'That time was just taken. Pick another.', code: 'slot_taken' },
            { status: 409 },
          )
        }
        if (moveRefused === 'not_offered') {
          return Response.json(
            {
              detail: [{ type: 'value_error', loc: ['body', 'starts_at'], msg: 'That time is no longer available. Pick another.' }],
              code: 'not_offered',
            },
            { status: 422 },
          )
        }
        const id = url.split('/').pop()
        const original = appointments.find((a) => a.id === id)
        const starts_at = body.starts_at ?? original.starts_at
        const duration_minutes = body.duration_minutes ?? original.duration_minutes
        // The server's row changes, so the next read of the grid says so too.
        Object.assign(original, {
          starts_at,
          duration_minutes,
          ends_at: new Date(new Date(starts_at).getTime() + duration_minutes * 60_000).toISOString(),
        })
        return Response.json(original)
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

const patches = (calls: Call[]) => calls.filter((c) => c.method === 'PATCH')

/** The page on the 15th of June, whatever the wall clock says. */
function onTheFifteenth() {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-06-15T14:32:00Z'))
}

beforeEach(() => {
  vi.unstubAllGlobals()
  toast.dismiss()
})

afterEach(() => vi.useRealTimers())

// --- the grid --------------------------------------------------------------------------------

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

test('an appointment sits in its staff column, one pixel per minute from midnight', async () => {
  onTheFifteenth()
  fakeServer({ appointments: [BOOKED, SHORT] })
  renderSchedule()

  const bo = await screen.findByRole('region', { name: 'Bo Chen' })
  const card = within(bo).getByTestId('event-a1')
  expect(card).toHaveAccessibleName('Priya Nair, Swedish Massage, 10:00 AM to 11:00 AM with Bo Chen')
  // 10:00 Toronto is 600 minutes into the day; an hour is 60 px.
  expect(card.parentElement).toHaveStyle({ top: '600px', height: '60px' })
  expect(within(bo).getByText('10:00 AM – 11:00 AM')).toBeInTheDocument()
  // A fifteen-minute event still shows a single clipped line with its title.
  const ana = screen.getByRole('region', { name: 'Ana Rossi' })
  expect(within(ana).getByTestId('event-a3').parentElement).toHaveStyle({ height: '15px' })
  expect(within(ana).getByText(/Quick Trim/)).toHaveClass('truncate')
  // The now-line is drawn at 10:32 in every column of today.
  expect(screen.getAllByTestId('now-line')).toHaveLength(2)
  expect(screen.getAllByTestId('now-line')[0]).toHaveStyle({ top: '632px' })
  // Shifts are lit: 09:00–17:00.
  expect(within(bo).getByTestId('shift')).toHaveStyle({ top: '540px', height: '480px' })
})

test('two overlapping appointments sit side by side at half width', async () => {
  onTheFifteenth()
  fakeServer({ appointments: [BOOKED, OVERLAPPING] })
  renderSchedule()

  const bo = await screen.findByRole('region', { name: 'Bo Chen' })
  const first = within(bo).getByTestId('event-a1').parentElement!
  const second = within(bo).getByTestId('event-a2').parentElement!
  expect(first).toHaveStyle({ left: 'calc(0% + 1px)', width: 'calc(50% - 2px)' })
  expect(second).toHaveStyle({ left: 'calc(50% + 1px)', width: 'calc(50% - 2px)' })
})

test('drawing on empty space opens the dialog prefilled with the person and the time', async () => {
  onTheFifteenth()
  fakeServer()
  const user = userEvent.setup()
  renderSchedule()

  const ana = await screen.findByRole('region', { name: 'Ana Rossi' })
  // jsdom lays nothing out, so the column's top is 0 and clientY is the y: 10:00 to 10:45.
  fireEvent.pointerDown(ana, { clientY: 600, button: 0, pointerId: 1, pointerType: 'mouse' })
  fireEvent.pointerMove(ana, { clientY: 640, pointerId: 1 })
  expect(screen.getByTestId('selection')).toHaveTextContent('10:00 AM – 10:45 AM')
  fireEvent.pointerUp(ana, { clientY: 640, pointerId: 1 })

  const dialog = await screen.findByRole('dialog', { name: 'New appointment' })
  expect(within(dialog).getByText('Drawn for 10:00 AM. Choose a service to book it.')).toBeInTheDocument()
  await user.click(within(dialog).getByRole('combobox', { name: 'Service' }))
  await user.click(await screen.findByRole('option', { name: /Swedish Massage/ }))
  await waitFor(() =>
    expect(within(dialog).getByRole('combobox', { name: 'Provider' })).toHaveTextContent('Ana Rossi'),
  )
  // The drawn time is the pick, as soon as the server offers it.
  await waitFor(() =>
    expect(within(dialog).getByRole('button', { name: '10:00 AM' })).toHaveAttribute('aria-pressed', 'true'),
  )
  expect(within(dialog).getByLabelText('Day')).toHaveValue(DAY)
})

test('a drawn time the server does not offer is said so', async () => {
  onTheFifteenth()
  fakeServer()
  const user = userEvent.setup()
  renderSchedule()

  const ana = await screen.findByRole('region', { name: 'Ana Rossi' })
  // 10:15 is Bo's alone.
  fireEvent.pointerDown(ana, { clientY: 615, button: 0, pointerId: 1, pointerType: 'mouse' })
  fireEvent.pointerUp(ana, { clientY: 615, pointerId: 1 })
  const dialog = await screen.findByRole('dialog', { name: 'New appointment' })
  await user.click(within(dialog).getByRole('combobox', { name: 'Service' }))
  await user.click(await screen.findByRole('option', { name: /Swedish Massage/ }))

  expect(await within(dialog).findByRole('status')).toHaveTextContent(
    '10:15 AM is not free for this service with Ana Rossi. Pick another time.',
  )
  expect(within(dialog).getByRole('button', { name: 'Book' })).toBeDisabled()
})

test('the keyboard moves by one step and Enter commits; Escape puts it back', async () => {
  onTheFifteenth()
  const calls = fakeServer({ appointments: [BOOKED] })
  const user = userEvent.setup()
  renderSchedule()

  const card = await screen.findByTestId('event-a1')
  card.focus()
  await user.keyboard('[Enter]')
  await user.keyboard('[ArrowDown]')
  // The ghost prints where the card is about to be, live.
  expect(screen.getByText('10:15 AM – 11:15 AM')).toBeInTheDocument()
  await user.keyboard('[Escape]')
  expect(patches(calls)).toHaveLength(0)
  expect(screen.queryByText('10:15 AM – 11:15 AM')).not.toBeInTheDocument()

  card.focus()
  await user.keyboard('[Enter]')
  await user.keyboard('[ArrowDown][ArrowDown]')
  await user.keyboard('[Enter]')
  await waitFor(() => expect(patches(calls)).toHaveLength(1))
  expect(patches(calls)[0]).toMatchObject({
    url: '/api/appointments/a1',
    body: { starts_at: '2026-06-15T14:30:00.000Z' },
  })
  // Landed there before the server answered, and stayed once it did.
  expect(await screen.findByText('10:30 AM – 11:30 AM')).toBeInTheDocument()
})

test('Shift and an arrow pull the end: a resize sends the new duration only', async () => {
  onTheFifteenth()
  const calls = fakeServer({ appointments: [BOOKED] })
  const user = userEvent.setup()
  renderSchedule()

  const card = await screen.findByTestId('event-a1')
  card.focus()
  await user.keyboard('[Enter]')
  await user.keyboard('{Shift>}[ArrowDown]{/Shift}')
  expect(screen.getByText('10:00 AM – 11:15 AM')).toBeInTheDocument()
  await user.keyboard('[Enter]')

  await waitFor(() => expect(patches(calls)).toHaveLength(1))
  expect(patches(calls)[0].body).toEqual({ duration_minutes: 75 })
})

test('the bottom edge is its own handle, and resizes from the keyboard too', async () => {
  onTheFifteenth()
  const calls = fakeServer({ appointments: [BOOKED] })
  const user = userEvent.setup()
  renderSchedule()

  const edge = await screen.findByTestId('resize-a1')
  edge.focus()
  await user.keyboard('[Enter][ArrowUp][Enter]')

  await waitFor(() => expect(patches(calls)).toHaveLength(1))
  expect(patches(calls)[0].body).toEqual({ duration_minutes: 45 })
})

test('nothing can be drawn while a card is picked up, and the drag still drops where it was', async () => {
  onTheFifteenth()
  const calls = fakeServer({ appointments: [BOOKED] })
  const user = userEvent.setup()
  renderSchedule()

  const card = await screen.findByTestId('event-a1')
  card.focus()
  await user.keyboard('[Enter][ArrowDown]')
  expect(screen.getByText('10:15 AM – 11:15 AM')).toBeInTheDocument()
  // A mouse-down on empty space now: no selection, no dialog on release.
  const ana = screen.getByRole('region', { name: 'Ana Rossi' })
  fireEvent.pointerDown(ana, { clientY: 600, button: 0, pointerId: 1, pointerType: 'mouse' })
  fireEvent.pointerMove(ana, { clientY: 640, pointerId: 1 })
  expect(screen.queryByTestId('selection')).not.toBeInTheDocument()
  fireEvent.pointerUp(ana, { clientY: 640, pointerId: 1 })
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  // The drag is untouched: Escape puts it back and nothing was sent.
  await user.keyboard('[Escape]')
  expect(screen.queryByText('10:15 AM – 11:15 AM')).not.toBeInTheDocument()
  expect(patches(calls)).toHaveLength(0)
  // And drawing works again once nothing is picked up.
  fireEvent.pointerDown(ana, { clientY: 600, button: 0, pointerId: 1, pointerType: 'mouse' })
  expect(screen.getByTestId('selection')).toBeInTheDocument()
  fireEvent.pointerUp(ana, { clientY: 600, pointerId: 1 })
  expect(await screen.findByRole('dialog', { name: 'New appointment' })).toBeInTheDocument()
})

test('the ghost keeps the packed width of the card it previews', async () => {
  onTheFifteenth()
  fakeServer({ appointments: [BOOKED, OVERLAPPING] })
  const user = userEvent.setup()
  renderSchedule()

  const card = await screen.findByTestId('event-a2')
  card.focus()
  await user.keyboard('[Enter][ArrowDown]')

  // The 10:30 card, one step down, at its own half of the column.
  const ghost = screen.getByText('10:45 AM – 11:45 AM').closest('.ring-2')!.parentElement!
  expect(ghost).toHaveStyle({ left: 'calc(50% + 1px)', width: 'calc(50% - 2px)' })
  await user.keyboard('[Escape]')
})

test('a refused move goes back where it was, with the reason on screen', async () => {
  onTheFifteenth()
  const calls = fakeServer({ appointments: [BOOKED], moveRefused: 'not_offered' })
  const user = userEvent.setup()
  renderSchedule()

  const card = await screen.findByTestId('event-a1')
  card.focus()
  await user.keyboard('[Enter][ArrowDown][Enter]')

  expect(await screen.findByText('Not moved: That time is no longer available. Pick another.')).toBeInTheDocument()
  await waitFor(() => expect(patches(calls)).toHaveLength(1))
  expect(screen.getByText('10:00 AM – 11:00 AM')).toBeInTheDocument()
  expect(screen.queryByText('10:15 AM – 11:15 AM')).not.toBeInTheDocument()
})

test('a lost race on a move is told the same way', async () => {
  onTheFifteenth()
  fakeServer({ appointments: [BOOKED], moveRefused: 'slot_taken' })
  const user = userEvent.setup()
  renderSchedule()

  const card = await screen.findByTestId('event-a1')
  card.focus()
  await user.keyboard('[Enter][ArrowDown][Enter]')

  expect(await screen.findByText('Not moved: That time was just taken. Pick another.')).toBeInTheDocument()
  expect(screen.getByText('10:00 AM – 11:00 AM')).toBeInTheDocument()
})

test('all-day time off and closures are pinned in the row above the grid', async () => {
  onTheFifteenth()
  fakeServer({
    timeOff: [
      // Ana away the whole of the 15th (midnight to midnight, Toronto); Bo out 14:00–15:00.
      { id: 't1', staff_id: 's1', all_day: true, reason: 'Dentist', starts_at: '2026-06-15T04:00:00Z', ends_at: '2026-06-16T04:00:00Z' },
      { id: 't2', staff_id: 's2', all_day: false, reason: null, starts_at: '2026-06-15T18:00:00Z', ends_at: '2026-06-15T19:00:00Z' },
    ],
    closures: [{ id: 'c1', date: DAY, name: 'Retreat' }],
  })
  renderSchedule()

  const anaRow = await screen.findByRole('list', { name: 'All day, Ana Rossi' })
  expect(within(anaRow).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['Closed · Retreat', 'Dentist'])
  const boRow = screen.getByRole('list', { name: 'All day, Bo Chen' })
  expect(within(boRow).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['Closed · Retreat'])
  // The timed one is hatched in the column, not pinned above.
  const bo = screen.getByRole('region', { name: 'Bo Chen' })
  expect(within(bo).getByTestId('time-off')).toHaveStyle({ top: '840px', height: '60px' })
})

test('the week view asks for Monday to Sunday and shows seven day columns', async () => {
  onTheFifteenth()
  const calls = fakeServer({ appointments: [BOOKED] })
  const user = userEvent.setup()
  renderSchedule()

  await screen.findByRole('region', { name: 'Bo Chen' })
  await user.click(screen.getByRole('tab', { name: 'Week' }))

  await waitFor(() =>
    expect(calls.some((c) => c.url === '/api/schedule?from=2026-06-15&to=2026-06-21')).toBe(true),
  )
  expect(await screen.findByRole('region', { name: 'Mon' })).toBeInTheDocument()
  expect(document.querySelectorAll('[data-column]')).toHaveLength(7)
  expect(within(screen.getByRole('region', { name: 'Mon' })).getByTestId('event-a1')).toBeInTheDocument()
  // Narrowing to one person asks the server for that person.
  await user.click(screen.getByRole('combobox', { name: 'Staff member' }))
  await user.click(await screen.findByRole('option', { name: 'Ana Rossi' }))
  await waitFor(() =>
    expect(calls.some((c) => c.url === '/api/schedule?from=2026-06-15&to=2026-06-21&staff_id=s1')).toBe(true),
  )
  await user.click(screen.getByRole('button', { name: 'Next week' }))
  expect(screen.getByLabelText('Day')).toHaveValue('2026-06-22')
})

// --- the dialog ------------------------------------------------------------------------------

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
  // The grid is read again so the new appointment shows up in its column.
  await waitFor(() =>
    expect(calls.filter((c) => c.url.startsWith('/api/schedule?')).length).toBeGreaterThan(1),
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
