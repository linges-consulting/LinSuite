import { QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { vi } from 'vitest'
import { Toaster } from '@/components/ui/sonner'
import { createQueryClient } from '@/lib/query-client'
import { ThemeProvider } from '@/lib/theme'
import { SchedulePage } from '@/routes/schedule'

/**
 * Shared fixtures for the card-menu transition tests (Task 18) — `*not*` itself a test
 * file, so each scenario can live alone in its own (see the `.test.tsx` files beside this
 * one, and their shared note on why one render per file).
 */

export const ROSTER = [
  { id: 's1', display_name: 'Ana Rossi', colour: 'blue', hex: '#1d4ed8', dark_hex: '#659dff', sort_order: 0, user_id: 'u1', max_concurrent_appointments: 1 },
  { id: 's2', display_name: 'Bo Chen', colour: 'teal', hex: '#0f766e', dark_hex: '#68b5ac', sort_order: 1, user_id: 'u2', max_concurrent_appointments: 2 },
]

export const TEN = '2026-06-15T14:00:00Z'
export const ELEVEN = '2026-06-15T15:00:00Z'
export const PRIYA = { id: 'c1', first_name: 'Priya', last_name: 'Nair', email: null, phone: '4165550199' }

export const BOOKED = {
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
  completed_at: null,
  cancelled_at: null,
  cancel_reason: null,
  no_show_at: null,
  overridden_rules: null,
  override_reason: null,
  customer: PRIYA,
  service: { id: 'v1', name: 'Swedish Massage' },
  staff: { id: 's2', display_name: 'Bo Chen', colour: 'teal' },
  resources: [{ id: 'r1', name: 'Room 1', kind: 'space' }],
}

export const GROUP_ID = 'g1'
export const LINK_A = { ...BOOKED, id: 'ga0', booking_group_id: GROUP_ID }
export const LINK_B = {
  ...BOOKED,
  id: 'ga1',
  booking_group_id: GROUP_ID,
  starts_at: ELEVEN,
  ends_at: '2026-06-15T15:30:00Z',
  staff: { id: 's1', display_name: 'Ana Rossi', colour: 'blue' },
}

export type Call = { url: string; method: string; body?: any }

export function fakeServer(appointments: any[] = []): Call[] {
  const calls: Call[] = []
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
      if (url === '/api/catalog/services') return Response.json({ services: [] })
      if (url.startsWith('/api/schedule?')) {
        const params = new URLSearchParams(url.split('?')[1])
        const includeCancelled = params.get('include_cancelled') === 'true'
        return Response.json({
          timezone: 'America/Toronto',
          granularity_minutes: 15,
          staff: ROSTER,
          working_blocks: [],
          time_off: [],
          closures: [],
          appointments: appointments.filter((a) => includeCancelled || a.status !== 'cancelled'),
        })
      }
      if (url.startsWith('/api/appointments/group/') && url.endsWith('/cancel') && method === 'POST') {
        const groupId = url.split('/')[4]
        const members = appointments.filter((a) => a.booking_group_id === groupId)
        for (const m of members) {
          if (m.status !== 'confirmed') continue
          Object.assign(m, {
            status: 'cancelled',
            cancelled_at: new Date().toISOString(),
            cancel_reason: body?.reason ?? null,
            resources: [],
          })
        }
        return Response.json({ booking_group_id: groupId, appointments: members })
      }
      if (url.startsWith('/api/appointments/') && method === 'POST') {
        const [, , , id, action] = url.split('/')
        const a = appointments.find((x) => x.id === id)
        if (!a) return Response.json({}, { status: 404 })
        if (a.status !== 'confirmed') {
          return Response.json(
            { detail: `A ${a.status} appointment cannot be changed that way.`, code: 'invalid_transition' },
            { status: 409 },
          )
        }
        if (action === 'complete') {
          Object.assign(a, { status: 'completed', completed_at: new Date().toISOString() })
          return Response.json(a)
        }
        if (action === 'no-show') {
          Object.assign(a, { status: 'no_show', no_show_at: new Date().toISOString(), resources: [] })
          return Response.json(a)
        }
        if (action === 'cancel') {
          Object.assign(a, {
            status: 'cancelled',
            cancelled_at: new Date().toISOString(),
            cancel_reason: body?.reason ?? null,
            resources: [],
          })
          return Response.json(a)
        }
      }
      return Response.json({}, { status: 404 })
    }),
  )
  return calls
}

export function renderSchedule() {
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

export function onTheFifteenth() {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-06-15T14:32:00Z'))
}
