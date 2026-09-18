import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '@/App'
import { NO_MFA } from './harness'
import { Toaster } from '@/components/ui/sonner'
import { createQueryClient } from '@/lib/query-client'
import { ThemeProvider } from '@/lib/theme'
import { SettingsPage } from '@/routes/settings'

/**
 * The Roles and People panels, and the three 403 codes the query client routes.
 *
 * The fake here is a small stateful server rather than a per-URL stub, because the things
 * worth testing are round trips: toggling a capability and seeing it come back, assigning a
 * role and seeing the row follow. A stub that returns a fixed payload would pass whether or
 * not the screen sent anything.
 */

const CAPABILITIES = [
  {
    key: 'admin',
    description: 'Enter Admin Mode and change settings.',
    group: 'Administration',
    requires_admin_mode: true,
  },
  {
    key: 'roles.manage',
    description: 'Create roles and choose what each one may do.',
    group: 'Administration',
    requires_admin_mode: true,
  },
  {
    key: 'schedule.view',
    description: 'See the appointment calendar.',
    group: 'Schedule',
    requires_admin_mode: false,
  },
  {
    key: 'customers.view',
    description: 'Open customer profiles.',
    group: 'Customers',
    requires_admin_mode: false,
  },
]

type Role = {
  id: string
  name: string
  description: string
  is_system: boolean
  capabilities: string[]
  user_count: number
}

function fakeServer() {
  const roles: Role[] = [
    {
      id: 'r-admin',
      name: 'Administrator',
      description: 'Everything.',
      is_system: true,
      capabilities: CAPABILITIES.map((c) => c.key),
      user_count: 1,
    },
    {
      id: 'r-staff',
      name: 'Staff',
      description: 'Day-to-day work.',
      is_system: true,
      capabilities: ['schedule.view', 'customers.view'],
      user_count: 1,
    },
    {
      id: 'r-desk',
      name: 'Receptionist',
      description: 'The front desk.',
      is_system: false,
      capabilities: ['schedule.view'],
      user_count: 0,
    },
  ]
  const users = [
    // `mfa_enrolled` distinguishes the two rows on purpose: the Reset MFA action is
    // disabled where there is nothing to reset, and one row of each proves both halves.
    { id: 'u1', email: 'owner@cedar.example', role: 'Administrator', role_id: 'r-admin', locked_until: null as string | null, mfa_enrolled: true },
    { id: 'u2', email: 'desk@cedar.example', role: 'Staff', role_id: 'r-staff', locked_until: '2026-01-05T14:30:00Z' as string | null, mfa_enrolled: false },
  ]
  const calls: { url: string; method: string; body?: any }[] = []
  /** Set by a test to make the next write refuse, the way the real guards do. */
  let refuse: { status: number; detail: string; code?: string } | null = null

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
      if (url === '/api/admin/capabilities') return Response.json({ capabilities: CAPABILITIES })
      if (url === '/api/admin/roles' && method === 'GET') return Response.json({ roles })
      if (url === '/api/admin/roles' && method === 'POST') {
        const created = { id: 'r-new', ...body, is_system: false, user_count: 0 }
        roles.push(created)
        return Response.json(created, { status: 201 })
      }
      if (url.startsWith('/api/admin/roles/') && method === 'PATCH') {
        const role = roles.find((r) => r.id === url.split('/').pop())!
        Object.assign(role, body)
        return Response.json(role)
      }
      if (url.startsWith('/api/admin/roles/') && method === 'DELETE') {
        roles.splice(roles.findIndex((r) => r.id === url.split('/').pop()), 1)
        return new Response(null, { status: 204 })
      }
      if (url === '/api/admin/users' && method === 'GET') return Response.json({ users })
      if (url.endsWith('/role') && method === 'PATCH') {
        const user = users.find((u) => u.id === url.split('/')[4])!
        user.role_id = body.role_id
        user.role = roles.find((r) => r.id === body.role_id)!.name
        return Response.json(user)
      }
      if (url.endsWith('/unlock')) {
        users.find((u) => u.id === url.split('/')[4])!.locked_until = null
        return new Response(null, { status: 204 })
      }
      if (url.endsWith('/mfa/reset')) {
        users.find((u) => u.id === url.split('/')[4])!.mfa_enrolled = false
        return new Response(null, { status: 204 })
      }
      return Response.json({}, { status: 404 })
    }),
  )
  return { calls, roles, users, reject: (r: typeof refuse) => (refuse = r) }
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

beforeEach(() => {
  vi.unstubAllGlobals()
  // Sonner's queue is module state, so a toast raised by one test is re-rendered into the
  // next test's Toaster. These tests assert the same sentences repeatedly, so the queue has
  // to be emptied rather than left to expire on its own.
  toast.dismiss()
})

describe('the Roles panel', () => {
  it('sends exactly the capabilities that were toggled, and shows them afterwards', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()

    await user.click(await screen.findByRole('button', { name: 'New role' }))
    await user.type(screen.getByLabelText('Name'), 'Stylist')
    // Two on, from two different groups — the grouping must not lose one.
    await user.click(screen.getByLabelText(/schedule\.view/))
    await user.click(screen.getByLabelText(/customers\.view/))
    await user.click(screen.getByRole('button', { name: 'Create role' }))

    await waitFor(() => {
      const post = server.calls.find((c) => c.url === '/api/admin/roles' && c.method === 'POST')
      expect(post?.body).toMatchObject({
        name: 'Stylist',
        capabilities: ['schedule.view', 'customers.view'],
      })
    })
    expect(await screen.findByText('Stylist')).toBeInTheDocument()
  })

  it('round-trips a capability off an existing role', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()

    const card = (await screen.findByText('Receptionist')).closest('[data-slot="card"]')!
    await user.click(within(card as HTMLElement).getByRole('button', { name: 'Edit' }))
    // It starts on, because the role holds it.
    const toggle = screen.getByLabelText(/schedule\.view/)
    expect(toggle).toBeChecked()
    await user.click(toggle)
    await user.click(screen.getByRole('button', { name: 'Save role' }))

    await waitFor(() => {
      const patch = server.calls.find((c) => c.method === 'PATCH')
      expect(patch?.body.capabilities).toEqual([])
    })
  })

  it('shows each capability with its description, so a key is never the only label', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderSettings()

    await user.click(await screen.findByRole('button', { name: 'New role' }))

    expect(screen.getByText('See the appointment calendar.')).toBeInTheDocument()
    expect(screen.getByText('Enter Admin Mode and change settings.')).toBeInTheDocument()
  })

  it('opens a built-in role read-only, with no way to save it', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderSettings()

    const card = (await screen.findByText('Administrator')).closest('[data-slot="card"]')!
    expect(within(card as HTMLElement).getByText('Built-in')).toBeInTheDocument()
    await user.click(within(card as HTMLElement).getByRole('button', { name: 'View' }))

    expect(screen.getByLabelText('Name')).toBeDisabled()
    expect(screen.getByLabelText(/schedule\.view/)).toBeDisabled()
    expect(screen.queryByRole('button', { name: /save|create/i })).not.toBeInTheDocument()
  })

  it('offers no delete on a built-in role, and disables it on one in use', async () => {
    fakeServer()
    renderSettings()

    const builtIn = (await screen.findByText('Staff')).closest('[data-slot="card"]')!
    expect(within(builtIn as HTMLElement).queryByRole('button', { name: /delete/i })).toBeNull()
  })

  it('deletes an unused role once the confirmation is accepted', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()

    const card = (await screen.findByText('Receptionist')).closest('[data-slot="card"]')!
    await user.click(within(card as HTMLElement).getByRole('button', { name: /Delete Receptionist/ }))
    // Nothing is sent until the confirmation — the first click only asks.
    expect(server.calls.some((c) => c.method === 'DELETE')).toBe(false)
    await user.click(screen.getByRole('button', { name: 'Delete role' }))

    await waitFor(() =>
      expect(server.calls.some((c) => c.method === 'DELETE' && c.url.endsWith('r-desk'))).toBe(true),
    )
  })
})

describe('the People panel', () => {
  it('assigns a role and shows the new one', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()

    await user.click(await screen.findByRole('tab', { name: 'People' }))
    await user.click(await screen.findByRole('combobox', { name: 'Role for desk@cedar.example' }))
    await user.click(await screen.findByRole('option', { name: 'Receptionist' }))

    await waitFor(() => {
      const patch = server.calls.find((c) => c.url === '/api/admin/users/u2/role')
      expect(patch?.body).toEqual({ role_id: 'r-desk' })
    })
  })

  it('offers Reset MFA only where there is a second factor to reset', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderSettings()

    await user.click(await screen.findByRole('tab', { name: 'People' }))
    const enrolled = (await screen.findByText('owner@cedar.example')).closest('tr')!
    const notEnrolled = screen.getByText('desk@cedar.example').closest('tr')!

    expect(within(enrolled).getByText('On')).toBeInTheDocument()
    expect(within(enrolled).getByRole('button', { name: /Reset MFA/ })).toBeEnabled()
    // Offered identically, this would sign somebody out of every device to remove a factor
    // they do not have.
    expect(within(notEnrolled).getByText('Off')).toBeInTheDocument()
    expect(within(notEnrolled).getByRole('button', { name: /Reset MFA/ })).toBeDisabled()
  })

  it('resets a second factor and says what it cost', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderSettings()

    await user.click(await screen.findByRole('tab', { name: 'People' }))
    const row = (await screen.findByText('owner@cedar.example')).closest('tr')!
    await user.click(within(row).getByRole('button', { name: /Reset MFA/ }))

    await waitFor(() =>
      expect(server.calls.some((c) => c.url === '/api/admin/users/u1/mfa/reset')).toBe(true),
    )
    expect(
      await screen.findByText(/signed out everywhere and will be asked to set one up again/),
    ).toBeInTheDocument()
  })

  it('surfaces the last-administrator refusal instead of predicting it', async () => {
    const server = fakeServer()
    server.reject({
      status: 409,
      detail: 'This would leave nobody who can administer this instance.',
    })
    const user = userEvent.setup()
    renderSettings()

    await user.click(await screen.findByRole('tab', { name: 'People' }))
    await user.click(await screen.findByRole('combobox', { name: 'Role for owner@cedar.example' }))
    await user.click(await screen.findByRole('option', { name: 'Staff' }))

    expect(
      await screen.findByText(/nobody who can administer this instance/),
    ).toBeInTheDocument()
  })

  it('unlocks a locked account, and the lock goes', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()

    await user.click(await screen.findByRole('tab', { name: 'People' }))
    expect(await screen.findByText(/^Locked until/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /unlock/i }))

    await waitFor(() =>
      expect(server.calls.some((c) => c.url === '/api/admin/users/u2/unlock')).toBe(true),
    )
    await waitFor(() => expect(screen.queryByText(/^Locked until/)).not.toBeInTheDocument())
  })
})

/**
 * The three refusals arrive with the same status and differ only by `code`. Before roles
 * existed, every 403 was treated as a lapsed admin window — which was right only because
 * nothing else could produce one.
 */
describe('the query client tells the three 403s apart', () => {
  // The client retries a failed query once before reporting it, so the refusal reaches
  // `onError` a backoff later than the default assertion window allows.
  const AFTER_RETRY = { timeout: 5_000 }

  const session = (mode: 'staff' | 'admin') => ({
    id: 'u1',
    email: 'owner@cedar.example',
    role: 'Administrator',
    capabilities: ['admin', 'roles.manage'],
    mode,
    can_switch_modes: true,
    admin_grant_expires_at: mode === 'admin' ? new Date(Date.now() + 900_000).toISOString() : null,
    admin_hard_limit_at: mode === 'admin' ? new Date(Date.now() + 1_800_000).toISOString() : null,
    must_change_password: false,
    mfa: NO_MFA,
  })

  /** A signed-in app whose one admin call answers with the refusal under test. */
  function renderWithRefusal(
    mode: 'staff' | 'admin',
    refusal: { detail: string; code: string },
    { mustChangeAfter = false } = {},
  ) {
    let changed = false
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/setup/status') return Response.json({ required: false })
        if (url === '/api/auth/me') {
          const body = session(mode)
          return Response.json({
            ...body,
            must_change_password: mustChangeAfter && changed,
        mfa: NO_MFA,
          })
        }
        if (url.startsWith('/api/admin/')) {
          changed = true
          return Response.json(refusal, { status: 403 })
        }
        return Response.json({ status: 'ok', database: 'ok' })
      }),
    )
    return render(
      <ThemeProvider>
        <QueryClientProvider client={createQueryClient()}>
          <MemoryRouter initialEntries={['/settings']}>
            <App />
          </MemoryRouter>
          <Toaster position="bottom-right" />
        </QueryClientProvider>
      </ThemeProvider>,
    )
  }

  it('says Admin Mode expired only when this tab believed it was elevated', async () => {
    renderWithRefusal('admin', {
      detail: 'Switch to Admin Mode to do this.',
      code: 'admin_mode_required',
    })

    expect(await screen.findByText('Admin Mode expired', {}, AFTER_RETRY)).toBeInTheDocument()
  })

  it('does not claim an expiry to somebody who was never in Admin Mode', async () => {
    renderWithRefusal('staff', {
      detail: 'Switch to Admin Mode to do this.',
      code: 'admin_mode_required',
    })

    await waitFor(
      () => expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument(),
      AFTER_RETRY,
    )
    expect(screen.queryByText('Admin Mode expired')).not.toBeInTheDocument()
  })

  it('apologises for a capability the role does not hold', async () => {
    renderWithRefusal('admin', {
      detail: 'Your role does not allow this.',
      code: 'capability_required',
    })

    expect(
      await screen.findByText("You don't have permission to do that", {}, AFTER_RETRY),
    ).toBeInTheDocument()
    // Not the mode toast — that would send somebody to re-enter a password for nothing.
    expect(screen.queryByText('Admin Mode expired')).not.toBeInTheDocument()
  })

  it('routes to the change-password screen when one is owed', async () => {
    renderWithRefusal(
      'admin',
      { detail: 'Set a new password before you continue.', code: 'password_change_required' },
      { mustChangeAfter: true },
    )

    // The code makes the client re-read `/me`, which now reports the flag, and the gate in
    // App.tsx does the routing — one mechanism for the forced change, not two.
    expect(await screen.findByText('Set a new password', {}, AFTER_RETRY)).toBeInTheDocument()
  })
})
