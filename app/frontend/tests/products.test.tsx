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
 * Settings → Products: the retail catalog (M4 #56).
 *
 * The fake is a small stateful server, the same shape `services.test.tsx` and
 * `resources.test.tsx` use — a product and its variants round-trip through several separate
 * calls (the product itself, and each variant one at a time), and that ordering is what is
 * worth testing here.
 */

type Row = Record<string, any>

function fakeServer() {
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
          quantity_on_hand: 40,
          low_stock_threshold: 5,
          tax_component_keys: [],
          active: true,
          sort_order: 0,
        },
      ],
    },
  ]
  const calls: { url: string; method: string; body?: any }[] = []
  let nextProductId = 1
  let nextVariantId = 1

  const findProduct = (url: string) => products.find((p) => p.id === url.split('/')[4])!
  const findVariant = (url: string) => {
    const product = findProduct(url)
    return { product, variant: product.variants.find((v: Row) => v.id === url.split('/')[6])! }
  }

  // #95: Add/Edit/Deactivate and Add variant are now gated on `catalog.manage`, held in Admin
  // Mode — this suite is about the catalog CRUD itself, not the gate, so the fake account
  // holds it outright rather than leaving `/api/auth/me` unanswered (which `useCan` reads as
  // "no capability" and would hide every action this file clicks).
  const account = () => ({
    id: 'u1',
    email: 'owner@cedar.example',
    role: 'Administrator',
    capabilities: ['catalog.manage'],
    mode: 'admin',
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
  })

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
        if (method === 'GET') {
          const all = url.includes('include_inactive=true')
          return Response.json({ products: products.filter((p) => all || p.active) })
        }
        if (method === 'POST' && url === '/api/admin/products') {
          const clash = products.some(
            (p) => p.name.toLowerCase() === String(body.name).toLowerCase(),
          )
          if (clash) {
            return Response.json(
              { detail: `A product named “${body.name}” already exists.` },
              { status: 409 },
            )
          }
          const created = { id: `p${++nextProductId}`, active: true, variants: [], ...body }
          products.push(created)
          return Response.json(created, { status: 201 })
        }
        if (method === 'PATCH') {
          Object.assign(findProduct(url), body)
          return Response.json(findProduct(url))
        }
        if (url.endsWith('/deactivate')) {
          findProduct(url).active = false
          return Response.json(findProduct(url))
        }
        if (url.endsWith('/reactivate')) {
          findProduct(url).active = true
          return Response.json(findProduct(url))
        }
      }

      if (url.endsWith('/receive') && method === 'POST') {
        const { product, variant } = findVariant(url)
        variant.quantity_on_hand = (variant.quantity_on_hand ?? 0) + body.quantity
        return Response.json(product)
      }

      if (url.includes('/variants')) {
        if (method === 'POST' && !url.endsWith('/deactivate') && !url.endsWith('/reactivate')) {
          const product = findProduct(url)
          const skuOrBarcodeClash = products
            .flatMap((p) => p.variants)
            .some(
              (v: Row) =>
                v.sku.toLowerCase() === String(body.sku).toLowerCase() ||
                (body.barcode && v.barcode?.toLowerCase() === String(body.barcode).toLowerCase()),
            )
          if (skuOrBarcodeClash) {
            return Response.json({ detail: `SKU “${body.sku}” is already in use.` }, { status: 409 })
          }
          const created = {
            id: `v${++nextVariantId}`,
            product_id: product.id,
            active: true,
            sort_order: 0,
            tax_component_keys: [],
            quantity_on_hand: 0,
            ...body,
          }
          product.variants.push(created)
          return Response.json(product)
        }
        if (method === 'PATCH') {
          const { product, variant } = findVariant(url)
          Object.assign(variant, body)
          return Response.json(product)
        }
        if (url.endsWith('/deactivate')) {
          const { product, variant } = findVariant(url)
          variant.active = false
          return Response.json(product)
        }
        if (url.endsWith('/reactivate')) {
          const { product, variant } = findVariant(url)
          variant.active = true
          return Response.json(product)
        }
      }

      if (url === '/api/admin/billing/tax-components') {
        const component = (id: string, code: string, name: string, province: string | null) => ({
          id,
          code,
          name,
          province,
          active: true,
          rates: [],
          current_rate_ppm: 50_000,
          applicable_to_business: true,
        })
        return Response.json({
          tax_components: [
            component('t1', 'GST', 'GST', null),
            component('t2', 'PST', 'PST', 'BC'),
          ],
        })
      }

      return Response.json({}, { status: 404 })
    }),
  )
  return { calls, products }
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

async function openProducts(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: 'Products' }))
  await screen.findByText('Shampoo')
}

async function openActions(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(await screen.findByRole('button', { name: `Actions for ${name}` }))
}

const submit = (label: string) => screen.getByRole('button', { name: label })

beforeEach(() => {
  vi.unstubAllGlobals()
  toast.dismiss()
})

// --- the table --------------------------------------------------------------------------

describe('the catalog', () => {
  it('shows the SKU, price and stock for each variant', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openProducts(user)

    const row = screen.getByText('500ml').closest('tr')!
    expect(within(row).getByText('SHMP-500')).toBeInTheDocument()
    expect(within(row).getByText('$24.99')).toBeInTheDocument()
    expect(within(row).getByText('40')).toBeInTheDocument()
  })

  it('flags a variant at or below its low-stock threshold', async () => {
    const server = fakeServer()
    server.products[0].variants[0].quantity_on_hand = 5
    const user = userEvent.setup()
    renderSettings()
    await openProducts(user)

    const row = screen.getByText('500ml').closest('tr')!
    expect(within(row).getByText('Low')).toBeInTheDocument()
  })
})

// --- creating a product ------------------------------------------------------------------

describe('creating a product', () => {
  it('adds it to the table', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openProducts(user)

    await user.click(screen.getByRole('button', { name: 'Add product' }))
    await user.type(screen.getByLabelText('Name'), 'Conditioner')
    await user.click(submit('Add product'))

    await waitFor(() => {
      const post = server.calls.find((c) => c.method === 'POST' && c.url === '/api/admin/products')
      expect(post?.body).toMatchObject({ name: 'Conditioner' })
    })
    expect(await screen.findByText('Conditioner')).toBeInTheDocument()
  })

  it('shows a duplicate name error', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openProducts(user)

    await user.click(screen.getByRole('button', { name: 'Add product' }))
    await user.type(screen.getByLabelText('Name'), 'shampoo')
    await user.click(submit('Add product'))

    const error = await screen.findByText(/already exists/)
    expect(error.closest('div')).toContainElement(screen.getByLabelText('Name'))
  })
})

// --- creating a variant ------------------------------------------------------------------

describe('creating a variant', () => {
  it('sends the price in cents, then the opening stock as a receipt', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openProducts(user)

    await openActions(user, 'Shampoo')
    await user.click(await screen.findByRole('menuitem', { name: 'Add variant' }))
    await user.type(screen.getByLabelText('Name'), '1L')
    await user.type(screen.getByLabelText('SKU'), 'SHMP-1000')
    await user.clear(screen.getByLabelText('Price'))
    await user.type(screen.getByLabelText('Price'), '39.99')
    await user.clear(screen.getByLabelText('Opening stock'))
    await user.type(screen.getByLabelText('Opening stock'), '12')
    await user.click(submit('Add variant'))

    await waitFor(() => {
      const receive = server.calls.find((c) => c.url.endsWith('/receive'))
      expect(receive).toMatchObject({
        url: '/api/admin/products/p1/variants/v2/receive',
        body: { quantity: 12, reason: 'Opening stock' },
      })
    })
    const post = server.calls.find((c) => c.url === '/api/admin/products/p1/variants')
    expect(post?.body).toEqual({
      name: '1L',
      sku: 'SHMP-1000',
      barcode: null,
      price_cents: 3999,
      low_stock_threshold: 0,
      tax_component_keys: [],
      tax_convention: 'exclusive',
    })
    expect(await screen.findByText('1L')).toBeInTheDocument()
  })

  it('toggles each tax component individually and states the price convention', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openProducts(user)

    await openActions(user, 'Shampoo')
    await user.click(await screen.findByRole('menuitem', { name: 'Add variant' }))
    await user.type(screen.getByLabelText('Name'), '1L')
    await user.type(screen.getByLabelText('SKU'), 'SHMP-1000')
    await user.click(await screen.findByRole('checkbox', { name: 'GST (GST)' }))
    await user.click(submit('Add variant'))

    await waitFor(() => {
      const post = server.calls.find((c) => c.url === '/api/admin/products/p1/variants')
      expect(post?.body).toMatchObject({ tax_component_keys: ['GST'], tax_convention: 'exclusive' })
    })
  })

  it('cannot be submitted with a negative stock count', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openProducts(user)

    await openActions(user, 'Shampoo')
    await user.click(await screen.findByRole('menuitem', { name: 'Add variant' }))
    await user.type(screen.getByLabelText('Name'), '1L')
    await user.type(screen.getByLabelText('SKU'), 'SHMP-1000')
    await user.clear(screen.getByLabelText('Opening stock'))
    await user.type(screen.getByLabelText('Opening stock'), '-1')

    expect(submit('Add variant')).toBeDisabled()
    expect(server.calls.some((c) => c.url === '/api/admin/products/p1/variants')).toBe(false)
  })

  it('shows a duplicate SKU error', async () => {
    fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openProducts(user)

    await openActions(user, 'Shampoo')
    await user.click(await screen.findByRole('menuitem', { name: 'Add variant' }))
    await user.type(screen.getByLabelText('Name'), 'Travel size')
    await user.type(screen.getByLabelText('SKU'), 'shmp-500')
    await user.click(submit('Add variant'))

    expect(await screen.findByText(/already in use/)).toBeInTheDocument()
  })
})

// --- editing and deactivating ------------------------------------------------------------

describe('editing a variant', () => {
  it('never sends a stock count — stock only moves through receive/adjust', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openProducts(user)

    await openActions(user, '500ml')
    await user.click(await screen.findByRole('menuitem', { name: 'Edit' }))
    expect(screen.queryByLabelText(/^(opening )?stock$/i)).not.toBeInTheDocument()
    await user.clear(screen.getByLabelText('Price'))
    await user.type(screen.getByLabelText('Price'), '19.99')
    await user.click(submit('Save variant'))

    await waitFor(() => {
      const patch = server.calls.find((c) => c.method === 'PATCH')
      expect(patch?.body).toMatchObject({ price_cents: 1999 })
      expect(patch?.body).not.toHaveProperty('quantity_on_hand')
    })
  })
})

describe('deactivating', () => {
  it('drops a variant from an active product without touching the product itself', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    renderSettings()
    await openProducts(user)

    await openActions(user, '500ml')
    await user.click(await screen.findByRole('menuitem', { name: 'Deactivate' }))

    await waitFor(() =>
      expect(
        server.calls.some((c) => c.url === '/api/admin/products/p1/variants/v1/deactivate'),
      ).toBe(true),
    )
    const row = (await screen.findByText('500ml')).closest('tr')!
    expect(within(row).getByText('Inactive')).toBeInTheDocument()
    expect(screen.getByText('Shampoo').closest('tr')!.className).not.toContain('opacity')
  })

  it('deactivates a product and brings it back behind the filter', async () => {
    const server = fakeServer()
    const user = userEvent.setup()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderSettings()
    await openProducts(user)

    await openActions(user, 'Shampoo')
    await user.click(await screen.findByRole('menuitem', { name: 'Deactivate' }))

    await waitFor(() =>
      expect(server.calls.some((c) => c.url === '/api/admin/products/p1/deactivate')).toBe(true),
    )
    await waitFor(() => expect(screen.queryByText('Shampoo')).not.toBeInTheDocument())

    await user.click(screen.getByLabelText('Show inactive'))
    expect(await screen.findByText('Shampoo')).toBeInTheDocument()
  })
})
