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
 * Settings → Resources: spaces and equipment.
 *
 * The fake is a small stateful server, for the same reason `staff.test.tsx`'s is — the
 * things worth testing here are round trips: creating a room and seeing it appear, renaming
 * it into a collision and seeing the field-level refusal, deactivating it and seeing it
 * greyed behind the filter.
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
]

type Row = Record<string, any>

function fakeServer() {
  const resources: Row[] = [
    {
      id: 'r1',
      kind: 'space',
      name: 'Room 1',
      description: 'Treatment room',
      colour: 'blue',
      active: true,
      sort_order: 0,
    },
    {
      id: 'r2',
      kind: 'equipment',
      name: 'Laser Unit',
      description: null,
      colour: null,
      active: true,
      sort_order: 0,
    },
    {
      id: 'r3',
      kind: 'space',
      name: 'Room 2',
      description: null,
      colour: null,
      active: true,
      sort_order: 1,
    },
  ]
  const calls: { url: string; method: string; body?: any }[] = []
  let nextId = 3

  const find = (url: string) => resources.find((r) => r.id === url.split('/')[4])!

  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      const body = init?.body ? JSON.parse(init.body as string) : undefined
      calls.push({ url, method, body })

      if (url === '/api/admin/staff/palette') return Response.json({ colours: PALETTE })
      if (url.startsWith('/api/admin/resources') && method === 'GET') {
        const query = new URLSearchParams(url.split('?')[1])
        const kind = query.get('kind')
        const all = query.get('include_inactive') === 'true'
        const filtered = resources.filter((r) => r.kind === kind && (all || r.active))
        return Response.json({ resources: filtered })
      }
      if (url === '/api/admin/resources' && method === 'POST') {
        const clash = resources.some(
          (r) => r.kind === body.kind && r.name.toLowerCase() === body.name.toLowerCase(),
        )
        if (clash) {
          return Response.json(
            { detail: `A ${body.kind} named “${body.name}” already exists.` },
            { status: 409 },
          )
        }
        const created = {
          id: `r${nextId++}`,
          active: true,
          sort_order: 0,
          description: null,
          colour: null,
          ...body,
        }
        resources.push(created)
        return Response.json(created, { status: 201 })
      }
      if (url.startsWith('/api/admin/resources/') && method === 'PATCH') {
        const row = find(url)
        const wantedName = 'name' in body ? body.name : row.name
        const clash = resources.some(
          (r) =>
            r.id !== row.id &&
            r.kind === row.kind &&
            r.name.toLowerCase() === String(wantedName).toLowerCase(),
        )
        if (clash) {
          return Response.json(
            { detail: `A ${row.kind} named “${wantedName}” already exists.` },
            { status: 409 },
          )
        }
        Object.assign(row, body)
        return Response.json(row)
      }
      if (url.endsWith('/deactivate')) {
        const row = find(url)
        row.active = false
        return Response.json(row)
      }
      if (url.endsWith('/reactivate')) {
        const row = find(url)
        row.active = true
        return Response.json(row)
      }
      return Response.json({}, { status: 404 })
    }),
  )
  return { calls, resources }
}

function renderResources() {
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

async function openResources(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('tab', { name: 'Resources' }))
}

async function openActions(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(await screen.findByRole('button', { name: `Actions for ${name}` }))
}

beforeEach(() => {
  vi.unstubAllGlobals()
  toast.dismiss()
})

describe('tabs', () => {
  it('shows spaces by default and switches to equipment', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderResources()
    await openResources(user)

    expect(await screen.findByText('Room 1')).toBeInTheDocument()
    expect(screen.queryByText('Laser Unit')).not.toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Equipment' }))

    expect(await screen.findByText('Laser Unit')).toBeInTheDocument()
    expect(screen.queryByText('Room 1')).not.toBeInTheDocument()
  })
})

describe('creating', () => {
  it('adds a space and shows it in the table', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderResources()
    await openResources(user)

    await user.click(await screen.findByRole('button', { name: 'Add space' }))
    await user.type(screen.getByLabelText('Name'), 'Room 4')
    await user.click(screen.getByRole('button', { name: 'Add space' }))

    await waitFor(() => {
      const post = server.calls.find((c) => c.url === '/api/admin/resources' && c.method === 'POST')
      expect(post?.body).toMatchObject({ kind: 'space', name: 'Room 4' })
    })
    expect(await screen.findByText('Room 4')).toBeInTheDocument()
  })

  it('adds equipment with a colour from the palette', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderResources()
    await openResources(user)
    await user.click(screen.getByRole('tab', { name: 'Equipment' }))

    await user.click(await screen.findByRole('button', { name: 'Add equipment' }))
    await user.type(screen.getByLabelText('Name'), 'Scanner')
    await user.click(screen.getByRole('button', { name: 'Teal' }))
    await user.click(screen.getByRole('button', { name: 'Add equipment' }))

    await waitFor(() => {
      const post = server.calls.find((c) => c.method === 'POST')
      expect(post?.body).toMatchObject({ kind: 'equipment', name: 'Scanner', colour: 'teal' })
    })
  })

  it('shows a duplicate name under the field instead of as a form-wide error', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderResources()
    await openResources(user)

    await user.click(await screen.findByRole('button', { name: 'Add space' }))
    await user.type(screen.getByLabelText('Name'), 'room 1')
    await user.click(screen.getByRole('button', { name: 'Add space' }))

    const error = await screen.findByText(/already exists/)
    // Attached to the field it is about, not floating as a general form error.
    expect(error.closest('div')).toContainElement(screen.getByLabelText('Name'))
    // The dialog is still open — nothing was silently closed on a refusal.
    expect(screen.getByLabelText('Name')).toHaveValue('room 1')
  })
})

describe('editing', () => {
  it('renames a resource', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderResources()
    await openResources(user)

    await openActions(user, 'Room 1')
    await user.click(await screen.findByRole('menuitem', { name: 'Edit' }))
    await user.clear(screen.getByLabelText('Name'))
    await user.type(screen.getByLabelText('Name'), 'Treatment Room A')
    await user.click(screen.getByRole('button', { name: 'Save space' }))

    await waitFor(() => {
      const patch = server.calls.find((c) => c.method === 'PATCH' && c.url.endsWith('/r1'))
      expect(patch?.body).toMatchObject({ name: 'Treatment Room A' })
    })
    expect(await screen.findByText('Treatment Room A')).toBeInTheDocument()
  })

  it('refuses a rename into an existing name under the field', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderResources()
    await openResources(user)

    // Room 2 already exists alongside Room 1; renaming it into a collision.
    await openActions(user, 'Room 2')
    await user.click(await screen.findByRole('menuitem', { name: 'Edit' }))
    await user.clear(screen.getByLabelText('Name'))
    await user.type(screen.getByLabelText('Name'), 'room 1')
    await user.click(screen.getByRole('button', { name: 'Save space' }))

    expect(await screen.findByText(/already exists/)).toBeInTheDocument()
  })
})

describe('deactivating', () => {
  it('greys the row behind the filter and reactivates it', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderResources()
    await openResources(user)

    await openActions(user, 'Room 1')
    await user.click(await screen.findByRole('menuitem', { name: 'Deactivate' }))

    await waitFor(() =>
      expect(server.calls.some((c) => c.url === '/api/admin/resources/r1/deactivate')).toBe(true),
    )
    await waitFor(() => expect(screen.queryByText('Room 1')).not.toBeInTheDocument())

    await user.click(screen.getByLabelText('Show inactive'))

    const row = (await screen.findByText('Room 1')).closest('tr')!
    expect(within(row).getByText('Inactive')).toBeInTheDocument()
    expect(row.className).toContain('opacity')

    await openActions(user, 'Room 1')
    await user.click(await screen.findByRole('menuitem', { name: 'Reactivate' }))

    await waitFor(() =>
      expect(server.calls.some((c) => c.url === '/api/admin/resources/r1/reactivate')).toBe(true),
    )
  })

  it('does nothing when the confirmation is declined', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderResources()
    await openResources(user)

    await openActions(user, 'Room 1')
    await user.click(await screen.findByRole('menuitem', { name: 'Deactivate' }))

    expect(server.calls.some((c) => c.url.endsWith('/deactivate'))).toBe(false)
    expect(screen.getByText('Room 1')).toBeInTheDocument()
  })
})
