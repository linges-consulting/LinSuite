import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Toaster } from '@/components/ui/sonner'
import { dayProblem, minutesToTime, nextBlock, timeToMinutes } from '@/lib/hours'
import { createQueryClient } from '@/lib/query-client'
import { ThemeProvider } from '@/lib/theme'
import { SettingsPage } from '@/routes/settings'

/**
 * Working hours, time off and closures.
 *
 * The fake is a small stateful server, like `resources.test.tsx`'s: what is worth testing
 * here is round trips — adding a second block to a Monday and seeing the split shift go up
 * as two blocks, dragging one into an overlap and being told before the request is made,
 * choosing all-day versus timed and seeing which payload that produces.
 *
 * The **payload** assertions are the point of the last two. A timed absence must go up as a
 * local datetime with no offset, because the server reads it against the business timezone;
 * a browser that helpfully appended `Z` would move somebody's dentist appointment.
 */

const PALETTE = [
  {
    key: 'blue',
    name: 'Blue',
    hex: '#1d4ed8',
    dark_hex: '#659dff',
    foreground: '#ffffff',
    dark_foreground: '#0f172a',
  },
]

const MEMBER = {
  id: 's1',
  user_id: 'u1',
  email: 'rae@cedar.example',
  role: 'Staff',
  role_id: 'role-staff',
  locked_until: null,
  mfa_enrolled: false,
  first_name: 'Rae',
  last_name: 'Okonkwo',
  display_name: 'Rae Okonkwo',
  is_practitioner: false,
  designation: null,
  licence_number: null,
  commission_rate_services_bp: 0,
  commission_rate_retail_bp: 0,
  colour: 'blue',
  max_concurrent_appointments: 1,
  active: true,
  sort_order: 0,
  invite_pending: false,
}

// The panel opens on the current year, so the fixture follows it rather than pinning a date
// and making the suite expire.
const YEAR = new Date().getFullYear()

// Ontario, as the server would answer for a business with `province = 'ON'`.
const STATUTORY = [
  { date: `${YEAR}-01-01`, name: "New Year's Day" },
  { date: `${YEAR}-07-01`, name: 'Canada Day' },
  { date: `${YEAR}-12-25`, name: 'Christmas Day' },
]

type Row = Record<string, any>

function fakeServer(options: { hours?: Row[]; timeOff?: Row[]; closures?: Row[] } = {}) {
  const hours: Row[] = options.hours ?? []
  const timeOff: Row[] = options.timeOff ?? []
  const closures: Row[] = options.closures ?? []
  const calls: { url: string; method: string; body?: any }[] = []
  let nextId = 1

  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      const body = init?.body ? JSON.parse(init.body as string) : undefined
      calls.push({ url, method, body })

      if (url === '/api/admin/staff/palette') return Response.json({ colours: PALETTE })
      if (url === '/api/admin/roles') {
        return Response.json({
          roles: [
            {
              id: 'role-staff',
              name: 'Staff',
              description: '',
              is_system: true,
              capabilities: [],
              user_count: 1,
            },
          ],
        })
      }
      if (url.startsWith('/api/admin/staff') && url.includes('/hours')) {
        if (method === 'PUT') {
          hours.length = 0
          hours.push(...body.blocks)
        }
        return Response.json({ blocks: hours })
      }
      if (url.startsWith('/api/admin/staff')) return Response.json({ staff: [MEMBER] })

      if (url.startsWith('/api/staff/s1/time-off')) {
        if (method === 'POST') {
          const created = {
            id: `t${nextId++}`,
            all_day: body.all_day,
            reason: body.reason,
            starts_at: '2026-07-01T04:00:00Z',
            ends_at: '2026-07-06T04:00:00Z',
            starts_at_local: body.all_day
              ? `${body.start_date}T00:00:00`
              : body.starts_at_local,
            ends_at_local: body.all_day ? `${body.end_date}T00:00:00` : body.ends_at_local,
            start_date: body.all_day ? body.start_date : body.starts_at_local.slice(0, 10),
            end_date: body.all_day
              ? (body.end_date ?? body.start_date)
              : body.ends_at_local.slice(0, 10),
          }
          timeOff.push(created)
          return Response.json(created, { status: 201 })
        }
        if (method === 'DELETE') {
          const id = url.split('/').pop()
          timeOff.splice(
            timeOff.findIndex((e) => e.id === id),
            1,
          )
          return new Response(null, { status: 204 })
        }
        return Response.json({ time_off: timeOff })
      }

      if (url.startsWith('/api/admin/closures')) {
        if (url.includes('import-statutory')) {
          const year = new URLSearchParams(url.split('?')[1]).get('year')
          let added = 0
          for (const holiday of STATUTORY) {
            if (!holiday.date.startsWith(String(year))) continue
            if (closures.some((c) => c.date === holiday.date)) continue
            closures.push({ id: `c${nextId++}`, ...holiday, source: 'statutory' })
            added += 1
          }
          return Response.json({ added, skipped: STATUTORY.length - added, closures })
        }
        if (method === 'POST') {
          if (closures.some((c) => c.date === body.date)) {
            return Response.json({ detail: `${body.date} is already closed.` }, { status: 409 })
          }
          const created = { id: `c${nextId++}`, ...body, source: 'manual' }
          closures.push(created)
          return Response.json(created, { status: 201 })
        }
        if (method === 'DELETE') {
          const id = url.split('/').pop()
          closures.splice(
            closures.findIndex((c) => c.id === id),
            1,
          )
          return new Response(null, { status: 204 })
        }
        const year = new URLSearchParams(url.split('?')[1]).get('year')
        return Response.json({ closures: closures.filter((c) => c.date.startsWith(year!)) })
      }

      return Response.json({}, { status: 404 })
    }),
  )
  return { calls, hours, timeOff, closures }
}

function renderSettings() {
  return render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter>
          <SettingsPage />
        </MemoryRouter>
        <Toaster position="bottom-right" />
      </QueryClientProvider>
    </ThemeProvider>,
  )
}

async function openHours(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('tab', { name: 'Staff' }))
  await user.click(await screen.findByRole('button', { name: `Actions for ${MEMBER.email}` }))
  await user.click(await screen.findByRole('menuitem', { name: 'Hours' }))
}

async function openTimeOff(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('tab', { name: 'Staff' }))
  await user.click(await screen.findByRole('button', { name: `Actions for ${MEMBER.email}` }))
  await user.click(await screen.findByRole('menuitem', { name: 'Time off' }))
}

/** `<input type="time">` rejects `type()`; the value is set the way a picker would. */
async function setTime(
  user: ReturnType<typeof userEvent.setup>,
  label: string,
  value: string,
) {
  await user.clear(screen.getByLabelText(label))
  await user.type(screen.getByLabelText(label), value)
}

beforeEach(() => {
  vi.unstubAllGlobals()
  toast.dismiss()
})

// --- the arithmetic, on its own ---------------------------------------------------------

describe('minutes and clock faces', () => {
  it('round-trips a time through minutes since local midnight', () => {
    expect(minutesToTime(540)).toBe('09:00')
    expect(timeToMinutes('09:00')).toBe(540)
    expect(minutesToTime(1080)).toBe('18:00')
  })

  it('reads midnight in an end field as the end of the day, not a zero-length block', () => {
    // A time input cannot type 24:00, and a shift that runs to midnight is ordinary.
    expect(timeToMinutes('00:00', true)).toBe(1440)
    expect(timeToMinutes('00:00')).toBe(0)
    expect(minutesToTime(1440)).toBe('00:00')
  })

  it('derives an added block from where the day already ends', () => {
    // The whole point: dropped onto an existing 09:00–17:00, a fixed afternoon draft would
    // land inside it and warn about an overlap the person did not make.
    expect(nextBlock([])).toEqual({ start: '09:00', end: '17:00' })
    expect(nextBlock([{ start: '09:00', end: '17:00' }])).toEqual({
      start: '18:00',
      end: '21:00',
    })
    expect(nextBlock([{ start: '09:00', end: '12:00' }])).toEqual({
      start: '13:00',
      end: '16:00',
    })
    // The latest end, not the last in the array — nothing sorts these.
    expect(
      nextBlock([
        { start: '14:00', end: '18:00' },
        { start: '09:00', end: '12:00' },
      ]),
    ).toEqual({ start: '19:00', end: '22:00' })
    // Never past midnight.
    expect(nextBlock([{ start: '09:00', end: '00:00' }]).end).toBe('00:00')
  })

  it('adds a block that does not immediately warn', () => {
    const day = [{ start: '09:00', end: '17:00' }]
    expect(dayProblem([...day, nextBlock(day)])).toBeNull()
  })

  it('names what is wrong with a day before the request is made', () => {
    expect(dayProblem([{ start: '09:00', end: '12:00' }])).toBeNull()
    // Touching is not overlapping: somebody who does not take lunch.
    expect(
      dayProblem([
        { start: '09:00', end: '12:00' },
        { start: '12:00', end: '15:00' },
      ]),
    ).toBeNull()
    expect(
      dayProblem([
        { start: '09:00', end: '12:00' },
        { start: '11:00', end: '15:00' },
      ]),
    ).toMatch(/overlap/)
    expect(dayProblem([{ start: '12:00', end: '09:00' }])).toMatch(/end after it starts/)
  })
})

// --- the weekly matrix -------------------------------------------------------------------

describe('working hours', () => {
  it('adds a block to a day and saves the whole week', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openHours(user)

    await user.click(await screen.findByRole('button', { name: 'Add a block to Monday' }))
    await user.click(screen.getByRole('button', { name: 'Save hours' }))

    await waitFor(() => {
      const put = server.calls.find((c) => c.method === 'PUT')
      expect(put?.body.blocks).toEqual([
        { weekday: 0, start_minute: 540, end_minute: 1020 },
      ])
    })
  })

  it('keeps a split shift as two blocks on one day', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openHours(user)

    await user.click(await screen.findByRole('button', { name: 'Add a block to Monday' }))
    await setTime(user, 'Monday block 1 end', '12:00')
    await user.click(screen.getByRole('button', { name: 'Add a block to Monday' }))
    await user.click(screen.getByRole('button', { name: 'Save hours' }))

    await waitFor(() => {
      const put = server.calls.find((c) => c.method === 'PUT')
      expect(put?.body.blocks).toEqual([
        { weekday: 0, start_minute: 540, end_minute: 720 },
        // 13:00–16:00: a break after the morning block's end, not a fixed afternoon.
        { weekday: 0, start_minute: 780, end_minute: 960 },
      ])
    })
  })

  it('removes a block', async () => {
    const server = fakeServer({
      hours: [
        { weekday: 0, start_minute: 540, end_minute: 720 },
        { weekday: 0, start_minute: 900, end_minute: 1080 },
      ],
    })
    const user = userEvent.setup()
    renderSettings()
    await openHours(user)

    await user.click(await screen.findByRole('button', { name: 'Remove Monday block 2' }))
    await user.click(screen.getByRole('button', { name: 'Save hours' }))

    await waitFor(() => {
      const put = server.calls.find((c) => c.method === 'PUT')
      expect(put?.body.blocks).toEqual([{ weekday: 0, start_minute: 540, end_minute: 720 }])
    })
  })

  it('warns about an overlap and refuses to submit it', async () => {
    const server = fakeServer({
      hours: [
        { weekday: 0, start_minute: 540, end_minute: 720 },
        { weekday: 0, start_minute: 900, end_minute: 1080 },
      ],
    })
    const user = userEvent.setup()
    renderSettings()
    await openHours(user)

    // Drag the morning block's end past the afternoon block's start.
    await setTime(user, 'Monday block 1 end', '16:00')

    expect(await screen.findByRole('alert')).toHaveTextContent(/overlap/)
    expect(screen.getByRole('button', { name: 'Save hours' })).toBeDisabled()
    expect(server.calls.some((c) => c.method === 'PUT')).toBe(false)
  })

  it('shows the days with no blocks as not working', async () => {
    fakeServer({ hours: [{ weekday: 0, start_minute: 540, end_minute: 720 }] })
    const user = userEvent.setup()
    renderSettings()
    await openHours(user)

    expect(await screen.findByLabelText('Monday block 1 start')).toHaveValue('09:00')
    expect(screen.getAllByText('Not working')).toHaveLength(6)
  })
})

// --- time off ------------------------------------------------------------------------------

describe('time off', () => {
  it('sends an all-day range as inclusive local dates', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openTimeOff(user)

    await user.type(await screen.findByLabelText('First day'), '2026-07-01')
    await user.type(screen.getByLabelText('Last day'), '2026-07-05')
    await user.type(screen.getByLabelText('Reason'), 'Vacation')
    await user.click(screen.getByRole('button', { name: 'Add time off' }))

    await waitFor(() => {
      const post = server.calls.find((c) => c.method === 'POST')
      expect(post?.body).toEqual({
        all_day: true,
        start_date: '2026-07-01',
        end_date: '2026-07-05',
        reason: 'Vacation',
      })
    })
    expect(await screen.findByText('2026-07-01 – 2026-07-05')).toBeInTheDocument()
  })

  it('leaves the end date out for a single day', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openTimeOff(user)

    await user.type(await screen.findByLabelText('First day'), '2026-07-01')
    await user.click(screen.getByRole('button', { name: 'Add time off' }))

    await waitFor(() => {
      const post = server.calls.find((c) => c.method === 'POST')
      expect(post?.body).toEqual({ all_day: true, start_date: '2026-07-01', reason: null })
    })
  })

  it('sends a timed absence as local datetimes with no offset', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openTimeOff(user)

    await user.click(await screen.findByLabelText('All day'))
    await user.type(screen.getByLabelText('From'), '2026-07-01T14:00')
    await user.type(screen.getByLabelText('Until'), '2026-07-01T16:30')
    await user.click(screen.getByRole('button', { name: 'Add time off' }))

    await waitFor(() => {
      const post = server.calls.find((c) => c.method === 'POST')
      expect(post?.body).toEqual({
        all_day: false,
        starts_at_local: '2026-07-01T14:00',
        ends_at_local: '2026-07-01T16:30',
        reason: null,
      })
      // Nothing that would make the server read it as an instant rather than a wall clock.
      expect(JSON.stringify(post?.body)).not.toContain('Z')
    })
  })

  it('removes an entry', async () => {
    const server = fakeServer({
      timeOff: [
        {
          id: 't9',
          all_day: true,
          reason: 'Vacation',
          starts_at: '2026-07-01T04:00:00Z',
          ends_at: '2026-07-02T04:00:00Z',
          starts_at_local: '2026-07-01T00:00:00',
          ends_at_local: '2026-07-02T00:00:00',
          start_date: '2026-07-01',
          end_date: '2026-07-01',
        },
      ],
    })
    const user = userEvent.setup()
    renderSettings()
    await openTimeOff(user)

    await user.click(await screen.findByRole('button', { name: 'Remove 2026-07-01' }))

    await waitFor(() =>
      expect(
        server.calls.some((c) => c.method === 'DELETE' && c.url.endsWith('/t9')),
      ).toBe(true),
    )
  })
})

// --- closures ---------------------------------------------------------------------------------

describe('closures', () => {
  async function openClosures(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole('tab', { name: 'Closures' }))
  }

  it('imports the statutory holidays for the year on screen', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openClosures(user)

    await user.click(
      await screen.findByRole('button', { name: `Import statutory holidays for ${YEAR}` }),
    )

    await waitFor(() =>
      expect(
        server.calls.some((c) => c.url.includes(`import-statutory?year=${YEAR}`)),
      ).toBe(true),
    )
    expect(await screen.findByText('Canada Day')).toBeInTheDocument()
    const row = screen.getByText('Canada Day').closest('tr')!
    expect(within(row).getByText('Statutory')).toBeInTheDocument()
  })

  it('adds a closure by hand and deletes one', async () => {
    const server = fakeServer({
      closures: [
        { id: 'c9', date: `${YEAR}-07-01`, name: 'Canada Day', source: 'statutory' },
      ],
    })
    const user = userEvent.setup()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderSettings()
    await openClosures(user)

    await user.click(await screen.findByRole('button', { name: 'Add closure' }))
    await user.type(screen.getByLabelText('Date'), `${YEAR}-08-05`)
    await user.type(screen.getByLabelText('Name'), 'Staff retreat')
    await user.click(screen.getAllByRole('button', { name: 'Add closure' }).at(-1)!)

    await waitFor(() => {
      const post = server.calls.find((c) => c.method === 'POST' && !c.url.includes('import'))
      expect(post?.body).toEqual({ date: `${YEAR}-08-05`, name: 'Staff retreat' })
    })

    await user.click(await screen.findByRole('button', { name: 'Remove Canada Day' }))

    await waitFor(() =>
      expect(server.calls.some((c) => c.method === 'DELETE' && c.url.endsWith('/c9'))).toBe(
        true,
      ),
    )
  })
})
