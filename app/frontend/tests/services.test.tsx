import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Toaster } from '@/components/ui/sonner'
import { centsToDollars, dollarsToCents } from '@/lib/money'
import { createQueryClient } from '@/lib/query-client'
import { ThemeProvider } from '@/lib/theme'
import { SettingsPage } from '@/routes/settings'

/**
 * Settings → Services: the catalog, and what delivering each thing in it needs.
 *
 * The fake is a small stateful server, like `resources.test.tsx`'s, because what is worth
 * testing here are round trips — and because the three sets a service carries are saved by
 * three separate calls that one Save has to order correctly.
 *
 * Money gets its own unit tests: it is the one conversion in this screen where being a cent
 * out is a real consequence rather than a cosmetic one.
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

const STAFF = [
  { id: 's1', display_name: 'Ana Rossi', colour: 'blue', active: true },
  { id: 's2', display_name: 'Bo Chen', colour: 'teal', active: true },
  { id: 's3', display_name: 'Gone Away', colour: 'blue', active: false },
]

const RESOURCES = [
  { id: 'r1', kind: 'space', name: 'Room 1', active: true },
  { id: 'r2', kind: 'space', name: 'Room 2', active: true },
  { id: 'r3', kind: 'equipment', name: 'Laser Unit 1', active: true },
  { id: 'r4', kind: 'equipment', name: 'Retired Scanner', active: false },
]

type Row = Record<string, any>

function fakeServer() {
  const services: Row[] = [
    {
      id: 'v1',
      name: 'Swedish Massage',
      description: 'Sixty minutes, full body.',
      duration_minutes: 60,
      buffer_before_minutes: 5,
      buffer_after_minutes: 15,
      price_cents: 12000,
      bookable_online: true,
      active: true,
      sort_order: 0,
      staff_ids: [],
      requirements: [],
    },
  ]
  const calls: { url: string; method: string; body?: any }[] = []
  let nextId = 1

  const find = (url: string) => services.find((s) => s.id === url.split('/')[4])!
  const named = (id: string) => RESOURCES.find((r) => r.id === id)?.name ?? null
  const active = (id: string) => RESOURCES.find((r) => r.id === id)?.active ?? null

  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      const body = init?.body ? JSON.parse(init.body as string) : undefined
      calls.push({ url, method, body })

      if (url === '/api/admin/staff/palette') return Response.json({ colours: PALETTE })
      // Both lists are asked for with `include_inactive=true`: the dialog has to be able to
      // name somebody who has left, not just the current roster.
      if (url.startsWith('/api/admin/staff')) return Response.json({ staff: STAFF })
      if (url.startsWith('/api/admin/resources')) return Response.json({ resources: RESOURCES })

      if (url.startsWith('/api/admin/services?') || url === '/api/admin/services') {
        if (method === 'GET') {
          const all = url.includes('include_inactive=true')
          return Response.json({ services: services.filter((s) => all || s.active) })
        }
        const clash = services.some(
          (s) => s.name.toLowerCase() === String(body.name).toLowerCase(),
        )
        if (clash) {
          return Response.json(
            { detail: `A service named “${body.name}” already exists.` },
            { status: 409 },
          )
        }
        const created = {
          id: `v${++nextId}`,
          active: true,
          staff_ids: [],
          requirements: [],
          ...body,
        }
        services.push(created)
        return Response.json(created, { status: 201 })
      }
      if (url.startsWith('/api/admin/services/') && method === 'PATCH') {
        Object.assign(find(url), body)
        return Response.json(find(url))
      }
      if (url.endsWith('/staff') && method === 'PUT') {
        const row = find(url)
        row.staff_ids = body.staff_ids
        return Response.json(row)
      }
      if (url.endsWith('/requirements') && method === 'PUT') {
        const row = find(url)
        row.requirements = body.requirements.map((r: Row) => ({
          ...r,
          resource_name: r.resource_id ? named(r.resource_id) : null,
          resource_active: r.resource_id ? active(r.resource_id) : null,
        }))
        return Response.json(row)
      }
      if (url.endsWith('/deactivate')) {
        find(url).active = false
        return Response.json(find(url))
      }
      if (url.endsWith('/reactivate')) {
        find(url).active = true
        return Response.json(find(url))
      }
      return Response.json({}, { status: 404 })
    }),
  )
  return { calls, services }
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

async function openServices(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: 'Services' }))
  await screen.findByText('Swedish Massage')
}

async function openActions(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(await screen.findByRole('button', { name: `Actions for ${name}` }))
}

/** The dialog's own submit, told apart from the table's button behind it by its label. */
const submit = (label: string) => screen.getByRole('button', { name: label })

beforeEach(() => {
  vi.unstubAllGlobals()
  toast.dismiss()
})

// --- money ------------------------------------------------------------------------------

describe('dollars and cents', () => {
  it('converts a typed amount to integer cents', () => {
    expect(dollarsToCents('120')).toBe(12000)
    expect(dollarsToCents('120.00')).toBe(12000)
    expect(dollarsToCents('99.95')).toBe(9995)
    expect(dollarsToCents('0.05')).toBe(5)
    expect(dollarsToCents('$1,250.50')).toBe(125050)
  })

  it('rounds half up on the third decimal, where a float would round down', () => {
    // `Math.round(12.005 * 100)` is 1200: the nearest double to 12.005 is below it.
    expect(dollarsToCents('12.005')).toBe(1201)
    expect(dollarsToCents('12.0049')).toBe(1200)
    expect(dollarsToCents('0.005')).toBe(1)
  })

  it('refuses what is not an amount, including a negative one', () => {
    expect(dollarsToCents('')).toBeNull()
    expect(dollarsToCents('   ')).toBeNull()
    expect(dollarsToCents('-5')).toBeNull()
    expect(dollarsToCents('twelve')).toBeNull()
    expect(dollarsToCents('1.2.3')).toBeNull()
  })

  it('accepts commas only where a thousands separator belongs', () => {
    expect(dollarsToCents('1,250')).toBe(125000)
    expect(dollarsToCents('1,250,000.99')).toBe(125000099)
    // A decimal comma. Stripping it would read $12.50 as $1,250 — a hundredfold overcharge
    // nobody typed, which is the worst thing this function could do.
    expect(dollarsToCents('12,5')).toBeNull()
    expect(dollarsToCents('1,23,456')).toBeNull()
    expect(dollarsToCents(',500')).toBeNull()
  })

  it('renders cents back as dollars', () => {
    expect(centsToDollars(12000)).toBe('120.00')
    expect(centsToDollars(5)).toBe('0.05')
    expect(centsToDollars(0)).toBe('0.00')
  })
})

// --- the table --------------------------------------------------------------------------

describe('the catalog', () => {
  it('shows the duration, the buffers and the price in dollars', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    const row = screen.getByText('Swedish Massage').closest('tr')!
    expect(within(row).getByText('60 min')).toBeInTheDocument()
    expect(within(row).getByText('+5 / +15 min')).toBeInTheDocument()
    expect(within(row).getByText('$120.00')).toBeInTheDocument()
    expect(within(row).getByText('Online')).toBeInTheDocument()
  })
})

// --- creating ---------------------------------------------------------------------------

describe('creating', () => {
  it('sends the price as integer cents', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    await user.click(screen.getByRole('button', { name: 'Add service' }))
    await user.type(screen.getByLabelText('Name'), 'Deep Tissue')
    await user.clear(screen.getByLabelText('Price'))
    await user.type(screen.getByLabelText('Price'), '99.95')
    await user.click(submit('Add service'))

    await waitFor(() => {
      const post = server.calls.find((c) => c.method === 'POST')
      expect(post?.body).toMatchObject({ name: 'Deep Tissue', price_cents: 9995 })
    })
    expect(await screen.findByText('Deep Tissue')).toBeInTheDocument()
  })

  it('cannot be submitted with a negative price', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    await user.click(screen.getByRole('button', { name: 'Add service' }))
    await user.type(screen.getByLabelText('Name'), 'Deep Tissue')
    await user.clear(screen.getByLabelText('Price'))
    await user.type(screen.getByLabelText('Price'), '-20')

    expect(submit('Add service')).toBeDisabled()
    expect(server.calls.some((c) => c.method === 'POST')).toBe(false)
  })

  it('refuses a duration that is not a five-minute step', async () => {
    const user = userEvent.setup()
    renderSettings()
    fakeServer()
    await openServices(user)

    await user.click(screen.getByRole('button', { name: 'Add service' }))
    await user.type(screen.getByLabelText('Name'), 'Deep Tissue')
    await user.clear(screen.getByLabelText('Duration'))
    await user.type(screen.getByLabelText('Duration'), '47')

    expect(await screen.findByText(/5-minute steps/)).toBeInTheDocument()
    expect(submit('Add service')).toBeDisabled()
  })

  it('shows a duplicate name under the field', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    await user.click(screen.getByRole('button', { name: 'Add service' }))
    await user.type(screen.getByLabelText('Name'), 'swedish massage')
    await user.click(submit('Add service'))

    const error = await screen.findByText(/already exists/)
    expect(error.closest('div')).toContainElement(screen.getByLabelText('Name'))
  })
})

// --- who may deliver it -------------------------------------------------------------------

describe('eligible staff', () => {
  it('saves the chosen staff as one replaced set', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    await openActions(user, 'Swedish Massage')
    await user.click(await screen.findByRole('menuitem', { name: 'Edit' }))
    await user.click(screen.getByLabelText('Ana Rossi'))
    await user.click(screen.getByLabelText('Bo Chen'))
    await user.click(submit('Save service'))

    await waitFor(() => {
      const put = server.calls.find((c) => c.url === '/api/admin/services/v1/staff')
      expect(put?.method).toBe('PUT')
      expect(put?.body).toEqual({ staff_ids: ['s1', 's2'] })
    })
    expect(await screen.findByText('2 staff')).toBeInTheDocument()
  })

  it('never offers a deactivated staff member', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    await user.click(screen.getByRole('button', { name: 'Add service' }))

    expect(screen.getByLabelText('Ana Rossi')).toBeInTheDocument()
    expect(screen.queryByLabelText('Gone Away')).not.toBeInTheDocument()
  })

  it('shows an assigned staff member who has left, and lets them be removed', async () => {
    const server = fakeServer()
    server.services[0].staff_ids = ['s1', 's3'] // s3 is deactivated
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    await openActions(user, 'Swedish Massage')
    await user.click(await screen.findByRole('menuitem', { name: 'Edit' }))

    // Named rather than silently pruned: the administrator has to see who left, and the
    // checkbox is disabled because re-assigning them is not on offer.
    const departed = screen.getByRole('checkbox', { name: 'Gone Away' })
    expect(departed).toBeDisabled()
    expect(screen.getByText(/deactivated/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Remove Gone Away' }))
    expect(screen.queryByRole('checkbox', { name: 'Gone Away' })).not.toBeInTheDocument()

    await user.click(submit('Save service'))

    // The departed id is gone from the payload, so the save is not a permanent 422.
    await waitFor(() => {
      const put = server.calls.find((c) => c.url === '/api/admin/services/v1/staff')
      expect(put?.body).toEqual({ staff_ids: ['s1'] })
    })
  })

  it('asks for the inactive staff too, or it could not name the one who left', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    expect(
      server.calls.some(
        (c) => c.url.startsWith('/api/admin/staff') && c.url.includes('include_inactive=true'),
      ),
    ).toBe(true)
  })

  it('leaves the staff set alone when it was not touched', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    await openActions(user, 'Swedish Massage')
    await user.click(await screen.findByRole('menuitem', { name: 'Edit' }))
    await user.clear(screen.getByLabelText('Price'))
    await user.type(screen.getByLabelText('Price'), '135')
    await user.click(submit('Save service'))

    await waitFor(() => expect(server.calls.some((c) => c.method === 'PATCH')).toBe(true))
    expect(server.calls.some((c) => c.url.endsWith('/staff') && c.method === 'PUT')).toBe(false)
  })
})

// --- what it needs --------------------------------------------------------------------------

describe('the requirements builder', () => {
  it('adds any space plus one particular device, and saves both', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    await user.click(screen.getByRole('button', { name: 'Add service' }))
    await user.type(screen.getByLabelText('Name'), 'Laser Facial')

    // Row 1 defaults to "any space", which is the majority case and needs no clicks.
    await user.click(screen.getByRole('button', { name: 'Add requirement' }))
    expect(screen.getByRole('combobox', { name: 'Requirement 1 resource' })).toHaveTextContent(
      'Any space',
    )

    // Row 2: equipment, and a named one.
    await user.click(screen.getByRole('button', { name: 'Add requirement' }))
    await user.click(screen.getByRole('combobox', { name: 'Requirement 2 kind' }))
    await user.click(await screen.findByRole('option', { name: 'Equipment' }))
    await user.click(screen.getByRole('combobox', { name: 'Requirement 2 resource' }))
    await user.click(await screen.findByRole('option', { name: 'Laser Unit 1' }))

    await user.click(submit('Add service'))

    await waitFor(() => {
      const put = server.calls.find((c) => c.url.endsWith('/requirements'))
      expect(put?.body).toEqual({
        requirements: [
          { kind: 'space', resource_id: null },
          { kind: 'equipment', resource_id: 'r3' },
        ],
      })
    })
  })

  it('only offers resources of that kind, and never a deactivated one', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    await user.click(screen.getByRole('button', { name: 'Add service' }))
    await user.click(screen.getByRole('button', { name: 'Add requirement' }))
    await user.click(screen.getByRole('combobox', { name: 'Requirement 1 resource' }))

    expect(await screen.findByRole('option', { name: 'Room 1' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'Laser Unit 1' })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'Retired Scanner' })).not.toBeInTheDocument()
  })

  it('drops the chosen resource when the kind changes under it', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    await user.click(screen.getByRole('button', { name: 'Add service' }))
    await user.type(screen.getByLabelText('Name'), 'Laser Facial')
    await user.click(screen.getByRole('button', { name: 'Add requirement' }))
    await user.click(screen.getByRole('combobox', { name: 'Requirement 1 resource' }))
    await user.click(await screen.findByRole('option', { name: 'Room 2' }))

    // The row now says equipment; keeping Room 2 selected would build a request the server
    // refuses.
    await user.click(screen.getByRole('combobox', { name: 'Requirement 1 kind' }))
    await user.click(await screen.findByRole('option', { name: 'Equipment' }))

    expect(screen.getByRole('combobox', { name: 'Requirement 1 resource' })).toHaveTextContent(
      'Any equipment',
    )

    await user.click(submit('Add service'))
    await waitFor(() => {
      const put = server.calls.find((c) => c.url.endsWith('/requirements'))
      expect(put?.body).toEqual({ requirements: [{ kind: 'equipment', resource_id: null }] })
    })
  })

  it('removes a row', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    await user.click(screen.getByRole('button', { name: 'Add service' }))
    await user.type(screen.getByLabelText('Name'), 'Laser Facial')
    await user.click(screen.getByRole('button', { name: 'Add requirement' }))
    await user.click(screen.getByRole('button', { name: 'Add requirement' }))
    await user.click(screen.getByRole('combobox', { name: 'Requirement 2 kind' }))
    await user.click(await screen.findByRole('option', { name: 'Equipment' }))

    await user.click(screen.getByRole('button', { name: 'Remove requirement 1' }))

    // The surviving row is the equipment one, renumbered.
    expect(screen.getByRole('combobox', { name: 'Requirement 1 kind' })).toHaveTextContent(
      'Equipment',
    )
    expect(
      screen.queryByRole('combobox', { name: 'Requirement 2 kind' }),
    ).not.toBeInTheDocument()

    await user.click(submit('Add service'))
    await waitFor(() => {
      const put = server.calls.find((c) => c.url.endsWith('/requirements'))
      expect(put?.body).toEqual({ requirements: [{ kind: 'equipment', resource_id: null }] })
    })
  })

  it('shows a requirement whose resource has been deactivated, and lets it be removed', async () => {
    const server = fakeServer()
    server.services[0].requirements = [
      { kind: 'space', resource_id: 'r1', resource_name: 'Room 1', resource_active: true },
      {
        kind: 'equipment',
        resource_id: 'r4',
        resource_name: 'Retired Scanner',
        resource_active: false,
      },
    ]
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    await openActions(user, 'Swedish Massage')
    await user.click(await screen.findByRole('menuitem', { name: 'Edit' }))

    // Read-only, not a select with nothing in it: the server refuses any save that still
    // names it, so the only useful offer is the truth and a way out.
    expect(
      screen.getByText((_, el) => el?.textContent === 'Retired Scanner — deactivated'),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('combobox', { name: 'Requirement 2 resource' }),
    ).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Remove requirement 2' }))
    await user.click(submit('Save service'))

    await waitFor(() => {
      const put = server.calls.find((c) => c.url.endsWith('/requirements'))
      expect(put?.body).toEqual({ requirements: [{ kind: 'space', resource_id: 'r1' }] })
    })
  })

  it('flags a deactivated resource in the table summary', async () => {
    const server = fakeServer()
    server.services[0].requirements = [
      {
        kind: 'equipment',
        resource_id: 'r4',
        resource_name: 'Retired Scanner',
        resource_active: false,
      },
    ]
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    const row = screen.getByText('Swedish Massage').closest('tr')!
    expect(within(row).getByText('Retired Scanner (deactivated)')).toBeInTheDocument()
  })

  it('prints what a service needs in the table', async () => {
    const server = fakeServer()
    server.services[0].requirements = [
      { kind: 'space', resource_id: null, resource_name: null, resource_active: null },
      {
        kind: 'equipment',
        resource_id: 'r3',
        resource_name: 'Laser Unit 1',
        resource_active: true,
      },
    ]
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    const row = screen.getByText('Swedish Massage').closest('tr')!
    expect(within(row).getByText('Any space, Laser Unit 1')).toBeInTheDocument()
  })
})

// --- editing and deactivating ----------------------------------------------------------------

describe('editing', () => {
  it('changes the price and shows the new one', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openServices(user)

    await openActions(user, 'Swedish Massage')
    await user.click(await screen.findByRole('menuitem', { name: 'Edit' }))
    expect(screen.getByLabelText('Price')).toHaveValue('120.00')
    await user.clear(screen.getByLabelText('Price'))
    await user.type(screen.getByLabelText('Price'), '135.50')
    await user.click(submit('Save service'))

    await waitFor(() => {
      const patch = server.calls.find((c) => c.method === 'PATCH')
      expect(patch?.body).toMatchObject({ price_cents: 13550 })
    })
    expect(await screen.findByText('$135.50')).toBeInTheDocument()
  })
})

describe('deactivating', () => {
  it('drops it from the list and brings it back behind the filter', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderSettings()
    await openServices(user)

    await openActions(user, 'Swedish Massage')
    await user.click(await screen.findByRole('menuitem', { name: 'Deactivate' }))

    await waitFor(() =>
      expect(
        server.calls.some((c) => c.url === '/api/admin/services/v1/deactivate'),
      ).toBe(true),
    )
    await waitFor(() => expect(screen.queryByText('Swedish Massage')).not.toBeInTheDocument())

    await user.click(screen.getByLabelText('Show inactive'))

    const row = (await screen.findByText('Swedish Massage')).closest('tr')!
    expect(within(row).getByText('Inactive')).toBeInTheDocument()
    expect(row.className).toContain('opacity')

    await openActions(user, 'Swedish Massage')
    await user.click(await screen.findByRole('menuitem', { name: 'Reactivate' }))

    await waitFor(() =>
      expect(server.calls.some((c) => c.url === '/api/admin/services/v1/reactivate')).toBe(true),
    )
  })
})
