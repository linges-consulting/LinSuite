import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from './harness'

/**
 * Settings → Products: per-variant stock Receive and Adjust (#100, spec #95's "Settings →
 * Products stock"). Both actions are gated through `useCan` — absent without their own
 * capability, and absent outside Admin Mode even when the capability is held.
 *
 * The fake session and product catalog live together here rather than reusing
 * `products.test.tsx`'s fake: that file never stubs `/api/auth/me`, so `useCan` is always
 * false there and Receive/Adjust never render — exactly the case that leaves its own tests
 * (creating and editing variants) undisturbed by this ticket.
 */

type Row = Record<string, any>

function fakeServer(opts: { capabilities?: string[]; mode?: 'staff' | 'admin' } = {}) {
  const capabilities = opts.capabilities ?? ['inventory.receive', 'inventory.adjust']
  const mode = opts.mode ?? 'admin'

  const products: Row[] = [
    {
      id: 'p1',
      name: 'Shampoo',
      description: 'Sulphate-free.',
      active: true,
      sort_order: 0,
      variants: [
        {
          id: 'v1',
          product_id: 'p1',
          name: '500ml',
          sku: 'SHMP-500',
          barcode: '0123456789012',
          price_cents: 2499,
          quantity_on_hand: 12,
          low_stock_threshold: 5,
          tax_component_keys: [],
          active: true,
          sort_order: 0,
        },
      ],
    },
  ]
  const calls: { url: string; method: string; body?: any }[] = []

  const account = () => ({
    id: 'u1',
    email: 'owner@cedar.example',
    role: 'Administrator',
    capabilities,
    mode,
    can_switch_modes: true,
    admin_grant_expires_at: mode === 'admin' ? new Date(Date.now() + 900_000).toISOString() : null,
    admin_hard_limit_at: mode === 'admin' ? new Date(Date.now() + 900_000).toISOString() : null,
    must_change_password: false,
    mfa: {
      enrolled: false,
      method: null,
      pending: false,
      enrolment_required: false,
      verified_at: null,
      email_otp_allowed: false,
    },
  })

  const findProduct = (url: string) => products.find((p) => p.id === url.split('/')[4])!
  const findVariant = (url: string) => {
    const product = findProduct(url)
    return { product, variant: product.variants.find((v: Row) => v.id === url.split('/')[6])! }
  }

  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      const body = init?.body ? JSON.parse(init.body as string) : undefined
      calls.push({ url, method, body })

      if (url === '/api/auth/me') return Response.json(account())
      if (url === '/api/branding') return Response.json({}, { status: 404 })
      if (url === '/api/setup/status') return Response.json({ required: false })

      if (url.startsWith('/api/admin/products') && !url.includes('/variants')) {
        if (method === 'GET') return Response.json({ products })
      }

      if (url.endsWith('/receive') && method === 'POST') {
        const { product, variant } = findVariant(url)
        variant.quantity_on_hand = (variant.quantity_on_hand ?? 0) + body.quantity
        return Response.json(product)
      }

      if (url.endsWith('/adjust') && method === 'POST') {
        const { product, variant } = findVariant(url)
        const next = variant.quantity_on_hand + body.quantity_delta
        if (next < 0) {
          return Response.json(
            { detail: 'This would take stock on hand below zero.' },
            { status: 409 },
          )
        }
        variant.quantity_on_hand = next
        return Response.json(product)
      }

      return Response.json({}, { status: 404 })
    }),
  )
  return { calls, products }
}

async function openProducts(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('tab', { name: 'Products' }))
  await screen.findByText('Shampoo')
}

async function openActions(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(await screen.findByRole('button', { name: `Actions for ${name}` }))
}

beforeEach(() => {
  vi.unstubAllGlobals()
  toast.dismiss()
})

describe('capability and mode gating', () => {
  it('offers Receive and Adjust to an admin holding both capabilities in Admin Mode', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderApp('/settings')
    await openProducts(user)
    await openActions(user, '500ml')

    expect(screen.getByRole('menuitem', { name: 'Receive stock' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Adjust stock' })).toBeInTheDocument()
  })

  it('withholds both without either capability', async () => {
    fakeServer({ capabilities: [] })
    const user = userEvent.setup()
    renderApp('/settings')
    await openProducts(user)
    await openActions(user, '500ml')

    expect(screen.queryByRole('menuitem', { name: 'Receive stock' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Adjust stock' })).not.toBeInTheDocument()
  })

  // #114 (spec #113 Staff Mode section): `/settings` is now a whole Admin-only route
  // (`RequireAdminMode` in `App.tsx`), so a Staff Mode visit never reaches the Products tab —
  // or Receive/Adjust — at all, rather than reaching it and finding the two menu items absent.
  it('withholds all of Settings, Products included, in Staff Mode', async () => {
    fakeServer({ mode: 'staff' })
    renderApp('/settings')

    expect(await screen.findByText('This area needs Admin Mode')).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: 'Products' })).not.toBeInTheDocument()
  })

  it('offers only the one capability actually held', async () => {
    fakeServer({ capabilities: ['inventory.receive'] })
    const user = userEvent.setup()
    renderApp('/settings')
    await openProducts(user)
    await openActions(user, '500ml')

    expect(screen.getByRole('menuitem', { name: 'Receive stock' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Adjust stock' })).not.toBeInTheDocument()
  })

  // #95: a receive-only lead can list and receive against products without `catalog.manage`
  // — the server now allows `GET /api/admin/products` for any of the three capabilities
  // (`RequiresAny`), and the panel must not offer catalog writes the server would refuse.
  it('offers a receive-only account the product and Receive, but no catalog writes', async () => {
    fakeServer({ capabilities: ['inventory.receive'] })
    const user = userEvent.setup()
    renderApp('/settings')
    await openProducts(user)

    expect(await screen.findByText('Shampoo')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add product' })).not.toBeInTheDocument()
    // The product row's own actions (Edit, Add variant, Deactivate) are all `catalog.manage`
    // — nothing there for this account, so the trigger itself is withheld.
    expect(screen.queryByRole('button', { name: 'Actions for Shampoo' })).not.toBeInTheDocument()

    await openActions(user, '500ml')

    expect(screen.getByRole('menuitem', { name: 'Receive stock' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Edit' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Deactivate' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Adjust stock' })).not.toBeInTheDocument()
  })
})

describe('receiving stock', () => {
  it('sends the quantity and an optional note, and refreshes the row', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderApp('/settings')
    await openProducts(user)
    await openActions(user, '500ml')
    await user.click(screen.getByRole('menuitem', { name: 'Receive stock' }))

    await user.type(await screen.findByLabelText('Quantity'), '8')
    await user.type(screen.getByLabelText('Note'), 'Weekly delivery')
    await user.click(screen.getByRole('button', { name: 'Receive stock' }))

    await waitFor(() => {
      const receive = server.calls.find((c) => c.url.endsWith('/receive'))
      expect(receive).toMatchObject({
        url: '/api/admin/products/p1/variants/v1/receive',
        body: { quantity: 8, reason: 'Weekly delivery' },
      })
    })
    const row = (await screen.findByText('500ml')).closest('tr')!
    expect(within(row).getByText('20')).toBeInTheDocument()
  })

  it('requires a quantity of at least one', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderApp('/settings')
    await openProducts(user)
    await openActions(user, '500ml')
    await user.click(screen.getByRole('menuitem', { name: 'Receive stock' }))

    await user.clear(await screen.findByLabelText('Quantity'))
    await user.type(screen.getByLabelText('Quantity'), '0')

    expect(screen.getByRole('button', { name: 'Receive stock' })).toBeDisabled()
  })
})

describe('adjusting stock', () => {
  it('shows the computed difference and sends the delta with the reason', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderApp('/settings')
    await openProducts(user)
    await openActions(user, '500ml')
    await user.click(screen.getByRole('menuitem', { name: 'Adjust stock' }))

    const counted = await screen.findByLabelText('Counted quantity')
    await user.clear(counted)
    await user.type(counted, '9')

    expect(await screen.findByText('On hand 12 → counted 9: -3')).toBeInTheDocument()

    await user.type(screen.getByLabelText('Reason'), 'Shelf recount')
    await user.click(screen.getByRole('button', { name: 'Save adjustment' }))

    await waitFor(() => {
      const adjust = server.calls.find((c) => c.url.endsWith('/adjust'))
      expect(adjust).toMatchObject({
        url: '/api/admin/products/p1/variants/v1/adjust',
        body: { quantity_delta: -3, reason: 'Shelf recount' },
      })
    })
    const row = (await screen.findByText('500ml')).closest('tr')!
    expect(within(row).getByText('9')).toBeInTheDocument()
  })

  it('requires a reason', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderApp('/settings')
    await openProducts(user)
    await openActions(user, '500ml')
    await user.click(screen.getByRole('menuitem', { name: 'Adjust stock' }))

    const counted = await screen.findByLabelText('Counted quantity')
    await user.clear(counted)
    await user.type(counted, '9')

    expect(screen.getByRole('button', { name: 'Save adjustment' })).toBeDisabled()
  })

  it('disables saving when the counted quantity has not actually changed', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderApp('/settings')
    await openProducts(user)
    await openActions(user, '500ml')
    await user.click(screen.getByRole('menuitem', { name: 'Adjust stock' }))

    await user.type(screen.getByLabelText('Reason'), 'No change')

    expect(screen.getByRole('button', { name: 'Save adjustment' })).toBeDisabled()
  })

  it('recomputes the difference and requires a second confirmation when on hand changed since opening', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderApp('/settings')
    await openProducts(user)
    await openActions(user, '500ml')
    await user.click(screen.getByRole('menuitem', { name: 'Adjust stock' }))

    const counted = await screen.findByLabelText('Counted quantity')
    await user.clear(counted)
    await user.type(counted, '9')
    await user.type(screen.getByLabelText('Reason'), 'Shelf recount')

    // Stock moved elsewhere (a sale) while the dialog was open.
    server.products[0].variants[0].quantity_on_hand = 10

    await user.click(screen.getByRole('button', { name: 'Save adjustment' }))

    // No adjust call yet — the stale value forced a recompute and a fresh confirmation instead.
    expect(server.calls.some((c) => c.url.endsWith('/adjust'))).toBe(false)
    expect(await screen.findByText('On hand 10 → counted 9: -1')).toBeInTheDocument()
    expect(screen.getByText(/changed since/i)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Confirm and save' }))

    await waitFor(() => {
      const adjust = server.calls.find((c) => c.url.endsWith('/adjust'))
      expect(adjust).toMatchObject({
        url: '/api/admin/products/p1/variants/v1/adjust',
        body: { quantity_delta: -1, reason: 'Shelf recount' },
      })
    })
  })
})
