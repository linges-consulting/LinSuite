import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Toaster } from '@/components/ui/sonner'
import { createQueryClient } from '@/lib/query-client'
import { ThemeProvider } from '@/lib/theme'
import { SettingsPage } from '@/routes/settings'

/**
 * Settings → Staff.
 *
 * The fake is a small stateful server, because the things worth testing here are round
 * trips: adding somebody and seeing the invited row appear, deactivating them and seeing
 * the roster change. A per-URL stub would pass whether or not the screen sent anything.
 *
 * Two rules get their own tests because the server enforces them and the screen must not
 * disagree: a practitioner needs a designation and a licence number, and commission is sent
 * in basis points however it is typed.
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
  {
    key: 'teal',
    name: 'Teal',
    hex: '#0f766e',
    dark_hex: '#68b5ac',
    foreground: '#ffffff',
    dark_foreground: '#0f172a',
  },
  {
    key: 'rose',
    name: 'Rose',
    hex: '#be123c',
    dark_hex: '#ff6e7c',
    foreground: '#ffffff',
    dark_foreground: '#0f172a',
  },
]

const ROLES = [
  {
    id: 'r-admin',
    name: 'Administrator',
    description: 'Everything.',
    is_system: true,
    capabilities: ['admin', 'users.manage'],
    user_count: 1,
  },
  {
    id: 'r-staff',
    name: 'Staff',
    description: 'Day-to-day work.',
    is_system: true,
    capabilities: ['schedule.view'],
    user_count: 1,
  },
]

type Row = Record<string, any>

function fakeServer() {
  const staff: Row[] = [
    {
      id: 's1',
      user_id: 'u1',
      email: 'owner@cedar.example',
      role: 'Administrator',
      role_id: 'r-admin',
      locked_until: null,
      // One row of each, so the Reset MFA gating proves both halves.
      mfa_enrolled: true,
      first_name: 'Ada',
      last_name: 'Okonkwo',
      display_name: 'Ada Okonkwo',
      is_practitioner: true,
      designation: 'RMT',
      licence_number: '#12345',
      commission_rate_services_bp: 4500,
      commission_rate_retail_bp: 1000,
      colour: 'blue',
      max_concurrent_appointments: 1,
      active: true,
      sort_order: 0,
      invite_pending: false,
    },
    {
      id: 's2',
      user_id: 'u2',
      email: 'desk@cedar.example',
      role: 'Staff',
      role_id: 'r-staff',
      locked_until: '2026-01-05T14:30:00Z',
      mfa_enrolled: false,
      first_name: 'Theo',
      last_name: 'Marsh',
      display_name: 'Theo Marsh',
      is_practitioner: false,
      designation: null,
      licence_number: null,
      commission_rate_services_bp: 0,
      commission_rate_retail_bp: 0,
      colour: 'teal',
      max_concurrent_appointments: 1,
      active: true,
      sort_order: 1,
      invite_pending: true,
    },
  ]
  const calls: { url: string; method: string; body?: any }[] = []
  /** Set by a test to make the next write refuse, the way the real guards do. */
  let refuse: { status: number; detail: string; code?: string } | null = null
  // What the deactivate response says is still booked ahead of the person being removed.
  let future = 0

  const find = (url: string) => staff.find((s) => s.id === url.split('/')[4])!

  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      const body = init?.body ? JSON.parse(init.body as string) : undefined
      calls.push({ url, method, body })

      if (refuse && method !== 'GET') {
        const { status, ...rest } = refuse
        refuse = null
        return Response.json(rest, { status })
      }
      if (url === '/api/admin/roles') return Response.json({ roles: ROLES })
      if (url === '/api/admin/staff/palette') return Response.json({ colours: PALETTE })
      if (url.startsWith('/api/admin/staff') && method === 'GET') {
        const all = url.includes('include_inactive=true')
        return Response.json({ staff: all ? staff : staff.filter((s) => s.active) })
      }
      if (url === '/api/admin/staff' && method === 'POST') {
        // The server chooses the colour when the form left it alone, and the account is
        // born with no password — which is what `invite_pending` says.
        const created = {
          id: 's3',
          user_id: 'u3',
          role: ROLES.find((r) => r.id === body.role_id)!.name,
          locked_until: null,
          mfa_enrolled: false,
          active: true,
          sort_order: 0,
          invite_pending: true,
          ...body,
          display_name: body.display_name ?? `${body.first_name} ${body.last_name}`,
          colour: body.colour ?? 'rose',
        }
        staff.push(created)
        return Response.json(created, { status: 201 })
      }
      if (url.startsWith('/api/admin/staff/') && method === 'PATCH') {
        Object.assign(find(url), body)
        return Response.json(find(url))
      }
      if (url.endsWith('/deactivate')) {
        const row = find(url)
        row.active = false
        return Response.json({ ...row, future_appointments: future })
      }
      if (url.endsWith('/reactivate')) {
        const row = find(url)
        row.active = true
        return Response.json(row)
      }
      if (url.endsWith('/resend-invite')) return Response.json({ status: 'sent' }, { status: 202 })
      if (url.endsWith('/role') && method === 'PATCH') {
        const row = staff.find((s) => s.user_id === url.split('/')[4])!
        row.role_id = body.role_id
        row.role = ROLES.find((r) => r.id === body.role_id)!.name
        return Response.json(row)
      }
      if (url.endsWith('/unlock')) {
        staff.find((s) => s.user_id === url.split('/')[4])!.locked_until = null
        return new Response(null, { status: 204 })
      }
      if (url.endsWith('/mfa/reset')) {
        staff.find((s) => s.user_id === url.split('/')[4])!.mfa_enrolled = false
        return new Response(null, { status: 204 })
      }
      return Response.json({}, { status: 404 })
    }),
  )
  return {
    calls,
    staff,
    reject: (r: typeof refuse) => (refuse = r),
    setFuture: (n: number) => (future = n),
  }
}

function renderStaff() {
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

/** Settings opens on Business; the Staff panel is a tab away. */
async function openStaff(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('tab', { name: 'Staff' }))
}

/** The row's actions live behind one menu, so every action opens it first. */
async function openActions(user: ReturnType<typeof userEvent.setup>, email: string) {
  await user.click(await screen.findByRole('button', { name: `Actions for ${email}` }))
}

beforeEach(() => {
  vi.unstubAllGlobals()
  // Sonner's queue is module state, so a toast raised by one test is re-rendered into the
  // next test's Toaster.
  toast.dismiss()
})

describe('adding a staff member', () => {
  it('sends the staff fields, converts the rates to basis points, and invites them', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderStaff()
    await openStaff(user)

    await user.click(await screen.findByRole('button', { name: 'Add staff member' }))
    await user.type(screen.getByLabelText('Email'), 'ana@cedar.example')
    await user.type(screen.getByLabelText('First name'), 'Ana')
    await user.type(screen.getByLabelText('Last name'), 'Rossi')
    await user.clear(screen.getByLabelText('Commission on services'))
    await user.type(screen.getByLabelText('Commission on services'), '42.5')
    await user.click(screen.getByRole('button', { name: 'Add and send invitation' }))

    await waitFor(() => {
      const post = server.calls.find(
        (c) => c.url === '/api/admin/staff' && c.method === 'POST',
      )
      expect(post?.body).toMatchObject({
        email: 'ana@cedar.example',
        first_name: 'Ana',
        last_name: 'Rossi',
        role_id: 'r-staff',
        is_practitioner: false,
        commission_rate_services_bp: 4250,
        // Left alone, so the server picks one.
        colour: null,
      })
    })
    // No password is collected, and none is sent: the invitation is the only way in.
    expect(screen.queryByLabelText(/password/i)).not.toBeInTheDocument()
    expect(
      await screen.findByText(/link to choose a password/i),
    ).toBeInTheDocument()
    expect(await screen.findByText('Ana Rossi')).toBeInTheDocument()
  })

  it('will not submit a practitioner without a designation and a licence number', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderStaff()
    await openStaff(user)

    await user.click(await screen.findByRole('button', { name: 'Add staff member' }))
    await user.type(screen.getByLabelText('Email'), 'ana@cedar.example')
    await user.type(screen.getByLabelText('First name'), 'Ana')
    await user.type(screen.getByLabelText('Last name'), 'Rossi')
    // The two fields do not exist until this is on — a receptionist has no college.
    expect(screen.queryByLabelText('Licence number')).not.toBeInTheDocument()
    await user.click(screen.getByLabelText('Practitioner'))

    const submit = screen.getByRole('button', { name: 'Add and send invitation' })
    expect(screen.getByLabelText('Licence number')).toBeInTheDocument()
    expect(submit).toBeDisabled()
    await user.type(screen.getByLabelText('Designation'), 'RMT')
    expect(submit).toBeDisabled()
    await user.type(screen.getByLabelText('Licence number'), '12345')
    expect(submit).toBeEnabled()

    await user.click(submit)
    await waitFor(() => {
      const post = server.calls.find((c) => c.method === 'POST')
      expect(post?.body).toMatchObject({ designation: 'RMT', licence_number: '12345' })
    })
  })

  it('sends the colour chosen from the palette', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderStaff()
    await openStaff(user)

    await user.click(await screen.findByRole('button', { name: 'Add staff member' }))
    await user.type(screen.getByLabelText('Email'), 'ana@cedar.example')
    await user.type(screen.getByLabelText('First name'), 'Ana')
    await user.type(screen.getByLabelText('Last name'), 'Rossi')
    // The role select fills its column rather than collapsing to its placeholder.
    expect(screen.getByLabelText('Role').className).toContain('w-full')
    await user.click(screen.getByRole('button', { name: 'Rose' }))
    expect(screen.getByRole('button', { name: 'Rose' })).toHaveAttribute('aria-pressed', 'true')
    await user.click(screen.getByRole('button', { name: 'Add and send invitation' }))

    await waitFor(() => {
      const post = server.calls.find((c) => c.method === 'POST')
      expect(post?.body.colour).toBe('rose')
    })
  })
})

describe('the roster', () => {
  it('names the calendar colour for a screen reader, not just as a dot', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderStaff()
    await openStaff(user)

    const practitioner = (await screen.findByText('Ada Okonkwo')).closest('tr')!
    // `title` on a span is not announced, so the swatch carries the name beside it.
    expect(within(practitioner).getByText('Calendar colour: Blue')).toBeInTheDocument()
    expect(within(screen.getByText('Theo Marsh').closest('tr')!).getByText(
      'Calendar colour: Teal',
    )).toBeInTheDocument()
  })

  it('shows the practitioner credentials that a receipt will carry', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderStaff()
    await openStaff(user)

    const practitioner = (await screen.findByText('Ada Okonkwo')).closest('tr')!
    expect(within(practitioner).getByText('RMT #12345')).toBeInTheDocument()
    expect(within(practitioner).getByText('45% · 10%')).toBeInTheDocument()

    const receptionist = screen.getByText('Theo Marsh').closest('tr')!
    expect(within(receptionist).queryByText(/RMT/)).toBeNull()
    // No password has ever been set on that account, and the row says so.
    expect(within(receptionist).getByText('Invited')).toBeInTheDocument()
  })

  it('sends the name instead of a null when the display name is cleared', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderStaff()
    await openStaff(user)

    await openActions(user, 'owner@cedar.example')
    await user.click(await screen.findByRole('menuitem', { name: 'Edit' }))
    await user.clear(screen.getByLabelText('Display name'))
    await user.click(screen.getByRole('button', { name: 'Save staff member' }))

    // The hint says "leave blank to use their name". A null would be a request to delete a
    // NOT NULL column; the name it falls back to is what the hint actually promises.
    await waitFor(() => {
      const patch = server.calls.find((c) => c.method === 'PATCH' && c.url.endsWith('/s1'))
      expect(patch?.body.display_name).toBe('Ada Okonkwo')
    })
  })

  it('edits a rate and sends only what changed', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderStaff()
    await openStaff(user)

    await openActions(user, 'owner@cedar.example')
    await user.click(await screen.findByRole('menuitem', { name: 'Edit' }))
    await user.clear(screen.getByLabelText('Commission on retail'))
    await user.type(screen.getByLabelText('Commission on retail'), '15')
    await user.click(screen.getByRole('button', { name: 'Save staff member' }))

    await waitFor(() => {
      const patch = server.calls.find((c) => c.method === 'PATCH' && c.url.endsWith('/s1'))
      expect(patch?.body).toMatchObject({
        commission_rate_retail_bp: 1500,
        commission_rate_services_bp: 4500,
      })
    })
  })

  it('assigns a role through the account endpoint, which is keyed on the account', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderStaff()
    await openStaff(user)

    await user.click(await screen.findByRole('combobox', { name: 'Role for desk@cedar.example' }))
    await user.click(await screen.findByRole('option', { name: 'Administrator' }))

    await waitFor(() => {
      const patch = server.calls.find((c) => c.url === '/api/admin/users/u2/role')
      expect(patch?.body).toEqual({ role_id: 'r-admin' })
    })
  })

  it('unlocks a locked account, and the lock goes', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderStaff()
    await openStaff(user)

    expect(await screen.findByText(/^Locked until/)).toBeInTheDocument()
    await openActions(user, 'desk@cedar.example')
    await user.click(await screen.findByRole('menuitem', { name: 'Unlock' }))

    await waitFor(() =>
      expect(server.calls.some((c) => c.url === '/api/admin/users/u2/unlock')).toBe(true),
    )
    await waitFor(() => expect(screen.queryByText(/^Locked until/)).not.toBeInTheDocument())
  })

  it('offers Reset MFA only where there is a second factor to reset', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderStaff()
    await openStaff(user)

    await openActions(user, 'desk@cedar.example')
    // Offered identically, this would sign somebody out of every device to remove a factor
    // they do not have.
    expect(await screen.findByRole('menuitem', { name: 'Reset MFA' })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
  })

  it('resends an invitation only while one is outstanding', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderStaff()
    await openStaff(user)

    await openActions(user, 'owner@cedar.example')
    expect(await screen.findByRole('menuitem', { name: 'Resend invite' })).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    await user.keyboard('{Escape}')

    await openActions(user, 'desk@cedar.example')
    await user.click(await screen.findByRole('menuitem', { name: 'Resend invite' }))

    await waitFor(() =>
      expect(server.calls.some((c) => c.url === '/api/admin/staff/s2/resend-invite')).toBe(true),
    )
    expect(await screen.findByText(/earlier link has stopped working/)).toBeInTheDocument()
  })
})

describe('deactivating', () => {
  it('confirms first, then drops them off the roster', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    const confirmed = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderStaff()
    await openStaff(user)

    await openActions(user, 'desk@cedar.example')
    await user.click(await screen.findByRole('menuitem', { name: 'Deactivate' }))
    // Refused at the confirmation: nothing was sent.
    expect(server.calls.some((c) => c.url.endsWith('/deactivate'))).toBe(false)

    confirmed.mockReturnValue(true)
    await openActions(user, 'desk@cedar.example')
    await user.click(await screen.findByRole('menuitem', { name: 'Deactivate' }))

    await waitFor(() =>
      expect(server.calls.some((c) => c.url === '/api/admin/staff/s2/deactivate')).toBe(true),
    )
    expect(await screen.findByText(/signed out everywhere. Their history is kept/)).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText('Theo Marsh')).not.toBeInTheDocument())
  })

  it('shows them greyed behind the filter, and brings them back', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderStaff()
    await openStaff(user)

    await openActions(user, 'desk@cedar.example')
    await user.click(await screen.findByRole('menuitem', { name: 'Deactivate' }))
    await waitFor(() => expect(screen.queryByText('Theo Marsh')).not.toBeInTheDocument())

    await user.click(screen.getByLabelText('Show inactive'))

    const row = (await screen.findByText('Theo Marsh')).closest('tr')!
    expect(within(row).getByText('Inactive')).toBeInTheDocument()
    expect(row.className).toContain('opacity')

    await openActions(user, 'desk@cedar.example')
    await user.click(await screen.findByRole('menuitem', { name: 'Reactivate' }))

    await waitFor(() =>
      expect(server.calls.some((c) => c.url === '/api/admin/staff/s2/reactivate')).toBe(true),
    )
    expect(await screen.findByText(/can sign in again/)).toBeInTheDocument()
  })

  it('says how many appointments the person still has when they are deactivated', async () => {
    const server = fakeServer()
    server.setFuture(2)
    const user = userEvent.setup()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderStaff()
    await openStaff(user)

    await openActions(user, 'desk@cedar.example')
    await user.click(await screen.findByRole('menuitem', { name: 'Deactivate' }))

    // Deactivating cancels nothing: the cards stay on the calendar and somebody has to
    // decide what happens to them.
    expect(await screen.findByText(/2 upcoming appointments/)).toBeInTheDocument()
  })

  it('surfaces the last-administrator refusal instead of predicting it', async () => {
    const server = fakeServer()
    server.reject({
      status: 409,
      detail: 'This would leave nobody who can administer this instance.',
    })
    const user = userEvent.setup()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderStaff()
    await openStaff(user)

    await openActions(user, 'owner@cedar.example')
    await user.click(await screen.findByRole('menuitem', { name: 'Deactivate' }))

    expect(await screen.findByText(/nobody who can administer this instance/)).toBeInTheDocument()
    expect(screen.getByText('Ada Okonkwo')).toBeInTheDocument()
  })
})
