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
 * Settings → Packages (#101, spec #95 user stories 57-59): package definitions against the
 * existing admin routes (`billing/packages.py`) — list, create, edit, deactivate, reactivate,
 * with per-item tax toggles, a tax convention and the transferable flag round-tripping the
 * same way `services.test.tsx` and `products.test.tsx` exercise theirs.
 *
 * `billing.manage` is administrative (requires Admin Mode), so this fake also answers
 * `/api/auth/me` — the one thing `services.test.tsx`'s fake does not need, because
 * `ServicesPanel` never calls `useCan`.
 */

const SERVICES = [
  { id: 'sv1', name: 'Massage', active: true },
  { id: 'sv2', name: 'Facial', active: true },
  { id: 'sv3', name: 'Retired Treatment', active: false },
]

const ADMIN_ACCOUNT = {
  id: 'u1',
  email: 'owner@cedar.example',
  role: 'Administrator',
  capabilities: ['billing.manage'],
  mode: 'admin' as const,
  can_switch_modes: true,
  admin_grant_expires_at: new Date(Date.now() + 900_000).toISOString(),
  admin_hard_limit_at: new Date(Date.now() + 900_000).toISOString(),
  must_change_password: false,
  mfa: {
    enrolled: false,
    method: null,
    pending: false,
    enrolment_required: false,
    verified_at: null,
    email_otp_allowed: false,
  },
}

type Row = Record<string, any>

function fakeServer(account: Row = ADMIN_ACCOUNT) {
  const packages: Row[] = [
    {
      id: 'pk1',
      name: '10-Session Massage Pack',
      description: 'Ten sixty-minute massages.',
      price_cents: 90000,
      expires_after_days: 365,
      transferable: false,
      tax_component_keys: [],
      tax_convention: 'exclusive',
      active: true,
      services: [
        { service_id: 'sv1', service_name: 'Massage', service_active: true, credits: 10 },
      ],
    },
  ]
  const calls: { url: string; method: string; body?: any }[] = []
  let nextId = 1

  const find = (url: string) => packages.find((p) => p.id === url.split('/')[4])!

  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      const body = init?.body ? JSON.parse(init.body as string) : undefined
      calls.push({ url, method, body })

      if (url === '/api/auth/me') return Response.json(account)

      if (url.startsWith('/api/admin/services')) {
        const all = url.includes('include_inactive=true')
        return Response.json({ services: SERVICES.filter((s) => all || s.active) })
      }
      if (url === '/api/admin/billing/tax-components') {
        const component = (id: string, code: string, name: string) => ({
          id,
          code,
          name,
          province: null,
          active: true,
          rates: [],
          current_rate_ppm: 50_000,
          applicable_to_business: true,
        })
        return Response.json({ tax_components: [component('t1', 'GST', 'GST')] })
      }

      if (url.startsWith('/api/admin/packages') && !url.includes('/services')) {
        if (method === 'GET') {
          const all = url.includes('include_inactive=true')
          return Response.json({ packages: packages.filter((p) => all || p.active) })
        }
        if (method === 'POST' && url === '/api/admin/packages') {
          const clash = packages.some(
            (p) => p.name.toLowerCase() === String(body.name).toLowerCase(),
          )
          if (clash) {
            return Response.json(
              { detail: `A package named “${body.name}” already exists.` },
              { status: 409 },
            )
          }
          const named = (id: string) => SERVICES.find((s) => s.id === id)
          const created = {
            id: `pk${++nextId}`,
            active: true,
            ...body,
            services: body.services.map((s: Row) => ({
              service_id: s.service_id,
              service_name: named(s.service_id)?.name ?? '?',
              service_active: named(s.service_id)?.active ?? true,
              credits: s.credits,
            })),
          }
          packages.push(created)
          return Response.json(created, { status: 201 })
        }
        if (method === 'PATCH') {
          Object.assign(find(url), body)
          return Response.json(find(url))
        }
        if (url.endsWith('/deactivate')) {
          find(url).active = false
          return Response.json(find(url))
        }
        if (url.endsWith('/reactivate')) {
          find(url).active = true
          return Response.json(find(url))
        }
      }
      if (url.endsWith('/services') && method === 'PUT') {
        const row = find(url)
        const named = (id: string) => SERVICES.find((s) => s.id === id)
        row.services = body.services.map((s: Row) => ({
          service_id: s.service_id,
          service_name: named(s.service_id)?.name ?? '?',
          service_active: named(s.service_id)?.active ?? true,
          credits: s.credits,
        }))
        return Response.json(row)
      }

      return Response.json({}, { status: 404 })
    }),
  )
  return { calls, packages }
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

async function openPackages(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: 'Packages' }))
}

async function openActions(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(await screen.findByRole('button', { name: `Actions for ${name}` }))
}

const submit = (label: string) => screen.getByRole('button', { name: label })

beforeEach(() => {
  vi.unstubAllGlobals()
  toast.dismiss()
})

// --- the mode/capability gate ------------------------------------------------------------

describe('the mode and capability gate', () => {
  it('is offered in Admin Mode to an account holding billing.manage', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openPackages(user)

    expect(await screen.findByText('10-Session Massage Pack')).toBeInTheDocument()
  })

  // #114 (spec #113 Staff Mode section): the placeholder is gone — the panel renders nothing
  // at all rather than a paragraph explaining why.
  it('renders nothing in Staff Mode even when the account holds billing.manage', async () => {
    fakeServer({ ...ADMIN_ACCOUNT, mode: 'staff' })
    const user = userEvent.setup()
    renderSettings()
    await openPackages(user)

    await waitFor(() =>
      expect(screen.queryByText('10-Session Massage Pack')).not.toBeInTheDocument(),
    )
    expect(screen.queryByText(/Admin Mode/)).not.toBeInTheDocument()
  })

  it('renders nothing for an account without billing.manage, even in Admin Mode', async () => {
    fakeServer({ ...ADMIN_ACCOUNT, capabilities: ['catalog.manage'] })
    const user = userEvent.setup()
    renderSettings()
    await openPackages(user)

    await waitFor(() =>
      expect(screen.queryByText('10-Session Massage Pack')).not.toBeInTheDocument(),
    )
    expect(screen.queryByText(/Admin Mode/)).not.toBeInTheDocument()
  })
})

// --- the table -----------------------------------------------------------------------------

describe('the list', () => {
  it('shows price, expiry, transferable and each service with its credits', async () => {
    const user = userEvent.setup()
    fakeServer()
    renderSettings()
    await openPackages(user)

    const row = (await screen.findByText('10-Session Massage Pack')).closest('tr')!
    expect(within(row).getByText('$900.00')).toBeInTheDocument()
    expect(within(row).getByText('365 days')).toBeInTheDocument()
    expect(within(row).getByText('No')).toBeInTheDocument()
    expect(within(row).getByText('Massage ×10')).toBeInTheDocument()
  })
})

// --- creating --------------------------------------------------------------------------

describe('creating a package', () => {
  it('round-trips price, expiry, transferable, tax and services', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openPackages(user)
    await screen.findByText('10-Session Massage Pack')

    await user.click(screen.getByRole('button', { name: 'Add package' }))
    await user.type(screen.getByLabelText('Name'), 'Facial 5-Pack')
    await user.clear(screen.getByLabelText('Price'))
    await user.type(screen.getByLabelText('Price'), '400')
    await user.type(screen.getByLabelText('Expires after'), '180')
    await user.click(screen.getByLabelText('Transferable', { exact: false }))
    await user.click(screen.getByLabelText('GST (GST)'))
    await user.click(screen.getByLabelText('Facial'))
    await user.clear(screen.getByLabelText('Facial credits'))
    await user.type(screen.getByLabelText('Facial credits'), '5')
    await user.click(submit('Add package'))

    await waitFor(() => {
      const post = server.calls.find((c) => c.method === 'POST' && c.url === '/api/admin/packages')
      expect(post?.body).toMatchObject({
        name: 'Facial 5-Pack',
        price_cents: 40000,
        expires_after_days: 180,
        transferable: true,
        tax_component_keys: ['GST'],
        services: [{ service_id: 'sv2', credits: 5 }],
      })
    })
    expect(await screen.findByText('Facial 5-Pack')).toBeInTheDocument()
  })

  it('cannot be submitted with no service selected', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openPackages(user)
    await screen.findByText('10-Session Massage Pack')

    await user.click(screen.getByRole('button', { name: 'Add package' }))
    await user.type(screen.getByLabelText('Name'), 'Nothing Yet')

    expect(submit('Add package')).toBeDisabled()
  })

  it('shows a duplicate name error under the field', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openPackages(user)
    await screen.findByText('10-Session Massage Pack')

    await user.click(screen.getByRole('button', { name: 'Add package' }))
    await user.type(screen.getByLabelText('Name'), '10-session massage pack')
    await user.click(screen.getByLabelText('Massage'))
    await user.click(submit('Add package'))

    const error = await screen.findByText(/already exists/)
    expect(error.closest('div')).toContainElement(screen.getByLabelText('Name'))
  })
})

// --- editing ---------------------------------------------------------------------------

describe('editing a package', () => {
  it('saves scalar changes without touching services when they are unchanged', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openPackages(user)
    await screen.findByText('10-Session Massage Pack')

    await openActions(user, '10-Session Massage Pack')
    await user.click(screen.getByRole('menuitem', { name: 'Edit' }))
    await user.clear(screen.getByLabelText('Price'))
    await user.type(screen.getByLabelText('Price'), '950')
    await user.click(submit('Save package'))

    await waitFor(() => {
      const patch = server.calls.find((c) => c.method === 'PATCH' && c.url === '/api/admin/packages/pk1')
      expect(patch?.body).toMatchObject({ price_cents: 95000 })
    })
    expect(server.calls.some((c) => c.url.endsWith('/services') && c.method === 'PUT')).toBe(false)
  })

  it('replaces the services set when credits change', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openPackages(user)
    await screen.findByText('10-Session Massage Pack')

    await openActions(user, '10-Session Massage Pack')
    await user.click(screen.getByRole('menuitem', { name: 'Edit' }))
    await user.clear(screen.getByLabelText('Massage credits'))
    await user.type(screen.getByLabelText('Massage credits'), '12')
    await user.click(submit('Save package'))

    await waitFor(() => {
      const put = server.calls.find(
        (c) => c.method === 'PUT' && c.url === '/api/admin/packages/pk1/services',
      )
      expect(put?.body).toMatchObject({ services: [{ service_id: 'sv1', credits: 12 }] })
    })
  })
})

// --- deactivate / reactivate ------------------------------------------------------------

describe('deactivating and reactivating', () => {
  it('round-trips both ways', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const user = userEvent.setup()
    const server = fakeServer()
    renderSettings()
    await openPackages(user)
    await screen.findByText('10-Session Massage Pack')

    await openActions(user, '10-Session Massage Pack')
    await user.click(screen.getByRole('menuitem', { name: 'Deactivate' }))

    await waitFor(() =>
      expect(server.calls.some((c) => c.url === '/api/admin/packages/pk1/deactivate')).toBe(true),
    )
    await waitFor(() =>
      expect(screen.queryByText('10-Session Massage Pack')).not.toBeInTheDocument(),
    )

    await user.click(screen.getByLabelText('Show inactive'))

    const row = (await screen.findByText('10-Session Massage Pack')).closest('tr')!
    expect(within(row).getByText('Inactive')).toBeInTheDocument()

    await openActions(user, '10-Session Massage Pack')
    await user.click(screen.getByRole('menuitem', { name: 'Reactivate' }))

    await waitFor(() =>
      expect(server.calls.some((c) => c.url === '/api/admin/packages/pk1/reactivate')).toBe(true),
    )
    await waitFor(() => expect(screen.queryByText('Inactive')).not.toBeInTheDocument())
  })
})
