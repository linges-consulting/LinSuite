import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * Sell (#106, spec #95 stories 42-50): start a sale with or without a client, resume an open
 * draft, search products and add them with a quantity, apply an eligible discount, and issue
 * — landing on the retail invoice with the payment dialog open and pre-filled (#103).
 */

afterEach(() => vi.unstubAllGlobals())

const ACCOUNT = (capabilities: string[]) => ({
  id: 'u2',
  email: 'desk@cedar.example',
  role: 'Staff',
  capabilities,
  mode: 'staff' as const,
  can_switch_modes: false,
  admin_grant_expires_at: null,
  admin_hard_limit_at: null,
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

const CATALOG = [
  {
    id: 'p1',
    name: 'Shampoo',
    description: null,
    variants: [
      {
        id: 'v1',
        name: '500ml',
        sku: 'SKU-1',
        barcode: null,
        price_cents: 2500,
        quantity_on_hand: 4,
        low_stock_threshold: 2,
        is_low_stock: false,
        tax_component_keys: ['gst'],
        tax_convention: 'exclusive' as const,
      },
    ],
  },
]

const DISCOUNT = {
  id: 'd1',
  name: 'Autumn 10%',
  kind: 'percentage' as const,
  percentage_bp: 1000,
  amount_cents: null,
  stackable: true,
}

type RawLine = { id: string; variant_id: string; quantity: number; unit_price_cents: number }
type Sale = {
  id: string
  status: 'draft' | 'issued'
  customer_id: string | null
  customer_name: string | null
  discount_ids: string[]
  lines: RawLine[]
  created_at: string
}

function priceLine(line: RawLine, discounted: boolean) {
  const gross = line.unit_price_cents * line.quantity
  const discount_cents = discounted ? Math.round(gross * 0.1) : 0
  const taxable = gross - discount_cents
  const tax_cents = Math.round(taxable * 0.05)
  const total = taxable + tax_cents
  return {
    id: line.id,
    variant_id: line.variant_id,
    quantity: line.quantity,
    unit_price_cents: line.unit_price_cents,
    tax_convention: 'exclusive' as const,
    discount_amounts: discount_cents > 0 ? { [DISCOUNT.id]: discount_cents } : {},
    discount_cents,
    tax: { pretax_cents: taxable, component_cents: { gst: tax_cents }, tax_cents, total_cents: total },
    line_total_cents: total,
  }
}

function saleOut(sale: Sale) {
  const discounted = sale.discount_ids.includes(DISCOUNT.id)
  const lines = sale.lines.map((l) => priceLine(l, discounted))
  const subtotal = lines.reduce((s, l) => s + l.unit_price_cents * l.quantity, 0)
  const discount_total = lines.reduce((s, l) => s + l.discount_cents, 0)
  const tax_total = lines.reduce((s, l) => s + l.tax.tax_cents, 0)
  return {
    id: sale.id,
    status: sale.status,
    customer_id: sale.customer_id,
    sold_by_staff_id: 'st1',
    payment_collector_staff_id: null,
    replaces_retail_invoice_id: null,
    lines,
    eligible_discounts: sale.lines.length > 0 ? [{ ...DISCOUNT, applied: discounted }] : [],
    discount_ids: sale.discount_ids,
    subtotal_cents: subtotal,
    discount_total_cents: discount_total,
    tax_totals_by_component: { gst: tax_total },
    tax_total_cents: tax_total,
    grand_total_cents: subtotal - discount_total + tax_total,
    created_at: sale.created_at,
    updated_at: sale.created_at,
  }
}

function sellStub(
  opts: {
    capabilities?: string[]
    seed?: Sale[]
    issueResponse?: { status: number; body: unknown }
  } = {},
) {
  const { capabilities = ['billing.view'], issueResponse } = opts
  const sales = new Map<string, Sale>((opts.seed ?? []).map((s) => [s.id, s]))
  let saleSeq = sales.size
  let lineSeq = 0
  const issued: string[] = []

  const api = stubApi({
    signedIn: true,
    respond: (url: string, body: any) => {
      if (url === '/api/auth/me') return Response.json(ACCOUNT(capabilities))
      if (url === '/api/catalog/products') return Response.json({ products: CATALOG })
      if (url.startsWith('/api/customers?')) {
        const q = new URL(url, 'http://test').searchParams.get('q') ?? ''
        const customers =
          q.toLowerCase().includes('priya')
            ? [{ id: 'c1', first_name: 'Priya', last_name: 'Nair', email: null, phone: null, classification: 'repeat' }]
            : []
        return Response.json({ customers })
      }
      if (url === '/api/retail-sales' && body !== undefined) {
        saleSeq += 1
        const id = `s${saleSeq}`
        const sale: Sale = {
          id,
          status: 'draft',
          customer_id: body.customer_id ?? null,
          customer_name: body.customer_id === 'c1' ? 'Priya Nair' : null,
          discount_ids: [],
          lines: [],
          created_at: new Date().toISOString(),
        }
        sales.set(id, sale)
        return Response.json(saleOut(sale), { status: 201 })
      }
      if (url === '/api/retail-sales' && body === undefined) {
        const open = [...sales.values()].filter((s) => s.status === 'draft')
        return Response.json({
          retail_sales: open.map((s) => ({
            id: s.id,
            customer_id: s.customer_id,
            customer_name: s.customer_name,
            sold_by_staff_id: 'st1',
            line_count: s.lines.length,
            created_at: s.created_at,
            updated_at: s.created_at,
          })),
        })
      }
      const linesMatch = url.match(/^\/api\/retail-sales\/([^/]+)\/lines$/)
      if (linesMatch) {
        const sale = sales.get(linesMatch[1])
        if (!sale) return Response.json({ detail: 'No such retail sale.' }, { status: 404 })
        const variant = CATALOG.flatMap((p) => p.variants).find((v) => v.id === body.variant_id)
        if (!variant) return Response.json({ detail: 'No such product variant.' }, { status: 404 })
        lineSeq += 1
        sale.lines.push({
          id: `l${lineSeq}`,
          variant_id: variant.id,
          quantity: body.quantity,
          unit_price_cents: variant.price_cents,
        })
        return Response.json(saleOut(sale), { status: 201 })
      }
      const removeLineMatch = url.match(/^\/api\/retail-sales\/([^/]+)\/lines\/([^/]+)$/)
      if (removeLineMatch) {
        const sale = sales.get(removeLineMatch[1])
        if (!sale) return Response.json({ detail: 'No such retail sale.' }, { status: 404 })
        sale.lines = sale.lines.filter((l) => l.id !== removeLineMatch[2])
        return Response.json(saleOut(sale))
      }
      const discountsMatch = url.match(/^\/api\/retail-sales\/([^/]+)\/discounts$/)
      if (discountsMatch) {
        const sale = sales.get(discountsMatch[1])
        if (!sale) return Response.json({ detail: 'No such retail sale.' }, { status: 404 })
        sale.discount_ids = body.discount_ids
        return Response.json(saleOut(sale))
      }
      const issueMatch = url.match(/^\/api\/retail-sales\/([^/]+)\/issue$/)
      if (issueMatch) {
        const sale = sales.get(issueMatch[1])
        if (!sale) return Response.json({ detail: 'No such retail sale.' }, { status: 404 })
        if (issueResponse) return Response.json(issueResponse.body, { status: issueResponse.status })
        sale.status = 'issued'
        issued.push(sale.id)
        const priced = saleOut(sale)
        return Response.json(
          {
            id: 'inv1',
            business_id: 1,
            invoice_number: 501,
            retail_sale_id: sale.id,
            customer_id: sale.customer_id,
            customer_name: sale.customer_name,
            status: 'issued',
            subtotal_cents: priced.subtotal_cents,
            discount_total_cents: priced.discount_total_cents,
            tax_total_cents: priced.tax_total_cents,
            tax_totals_by_component: priced.tax_totals_by_component,
            grand_total_cents: priced.grand_total_cents,
            sold_by_staff_id: 'st1',
            payment_collector_staff_id: null,
            issued_at: new Date().toISOString(),
            issued_by: 'u2',
            lines: priced.lines.map((l) => ({
              id: l.id,
              variant_id: l.variant_id,
              variant_name: 'Shampoo — 500ml',
              quantity: l.quantity,
              unit_price_cents: l.unit_price_cents,
              discount_cents: l.discount_cents,
              tax_cents: l.tax.tax_cents,
              tax_convention: l.tax_convention,
              line_total_cents: l.line_total_cents,
              discounts: [],
              taxes: [],
              staff_id: 'st1',
            })),
            outstanding_cents: priced.grand_total_cents,
            refunded_cents: 0,
            checkout_complete: false,
            held_credit_cents: 0,
            replaces_invoice_id: null,
            replaced_by_invoice_id: null,
            cancelled_at: null,
            cancel_reason: null,
          },
          { status: 201 },
        )
      }
      const saleMatch = url.match(/^\/api\/retail-sales\/([^/]+)$/)
      if (saleMatch && body === undefined) {
        const sale = sales.get(saleMatch[1])
        if (!sale) return Response.json({ detail: 'No such retail sale.' }, { status: 404 })
        return Response.json(saleOut(sale))
      }
      if (url === '/api/retail-invoices/inv1') {
        const sale = [...sales.values()].find((s) => s.status === 'issued')
        const priced = sale ? saleOut(sale) : null
        return Response.json({
          id: 'inv1',
          business_id: 1,
          invoice_number: 501,
          retail_sale_id: sale?.id ?? 's1',
          customer_id: sale?.customer_id ?? null,
          customer_name: sale?.customer_name ?? null,
          status: 'issued',
          subtotal_cents: priced?.subtotal_cents ?? 0,
          discount_total_cents: priced?.discount_total_cents ?? 0,
          tax_total_cents: priced?.tax_total_cents ?? 0,
          tax_totals_by_component: priced?.tax_totals_by_component ?? {},
          grand_total_cents: priced?.grand_total_cents ?? 2625,
          sold_by_staff_id: 'st1',
          payment_collector_staff_id: null,
          issued_at: new Date().toISOString(),
          issued_by: 'u2',
          lines: [],
          outstanding_cents: priced?.grand_total_cents ?? 2625,
          refunded_cents: 0,
          checkout_complete: false,
          held_credit_cents: 0,
          replaces_invoice_id: null,
          replaced_by_invoice_id: null,
          cancelled_at: null,
          cancel_reason: null,
        })
      }
      if (url === '/api/retail-invoices/inv1/payments') {
        if (body !== undefined) {
          return Response.json(
            {
              id: 'pay1',
              invoice_id: null,
              retail_invoice_id: 'inv1',
              payer_type: body.payer_type,
              method: body.method,
              amount_cents: body.amount_cents,
              status: body.status,
              reference: body.reference,
              recorded_at: new Date().toISOString(),
              recorded_by: 'u2',
              superseded_by: null,
              correction_reason: null,
            },
            { status: 201 },
          )
        }
        return Response.json({ payments: [] })
      }
      if (url === '/api/retail-invoices/inv1/refunds') return Response.json({ refunds: [] })
      if (url === '/api/retail-invoices/inv1/balance-exceptions') return Response.json({ exceptions: [] })
      return undefined
    },
  })
  return { ...api, issued }
}

// --- nav gating -----------------------------------------------------------------------------

test('the Sell nav entry is offered when billing.view is held', async () => {
  sellStub()
  renderApp('/')

  expect(await screen.findByRole('link', { name: 'Sell' })).toBeInTheDocument()
})

test('without billing.view there is no Sell nav entry', async () => {
  sellStub({ capabilities: ['schedule.view'] })
  renderApp('/')

  await screen.findByRole('link', { name: 'Schedule' })
  await waitFor(() => expect(screen.queryByRole('link', { name: 'Sell' })).not.toBeInTheDocument())
})

// --- starting a sale, with and without a client ----------------------------------------------

test('starting a walk-in sale opens a cart with no client', async () => {
  const user = userEvent.setup()
  sellStub()
  renderApp('/sell')

  await user.click(await screen.findByRole('button', { name: 'Start walk-in sale' }))

  expect(await screen.findByRole('heading', { name: 'Walk-in' })).toBeInTheDocument()
})

test('starting a sale with a chosen client carries the client through', async () => {
  const user = userEvent.setup()
  sellStub()
  renderApp('/sell')

  await user.type(await screen.findByPlaceholderText('Name, phone or email'), 'Priya')
  await user.click(await screen.findByRole('button', { name: 'Priya Nair' }))
  await user.click(screen.getByRole('button', { name: 'Start sale for Priya Nair' }))

  expect(await screen.findByRole('heading', { name: 'Priya Nair' })).toBeInTheDocument()
})

// --- resuming an open draft, including after a reload ------------------------------------------

test('an open draft is listed and resumable', async () => {
  const user = userEvent.setup()
  sellStub({
    seed: [
      {
        id: 's1',
        status: 'draft',
        customer_id: null,
        customer_name: null,
        discount_ids: [],
        lines: [{ id: 'l1', variant_id: 'v1', quantity: 2, unit_price_cents: 2500 }],
        created_at: '2026-09-27T14:00:00Z',
      },
    ],
  })
  renderApp('/sell')

  await user.click(await screen.findByRole('button', { name: /Walk-in/ }))

  expect(await screen.findByText('500ml')).toBeInTheDocument()
})

test('a draft is resumed by its own URL after a reload', async () => {
  sellStub({
    seed: [
      {
        id: 's1',
        status: 'draft',
        customer_id: null,
        customer_name: null,
        discount_ids: [],
        lines: [{ id: 'l1', variant_id: 'v1', quantity: 1, unit_price_cents: 2500 }],
        created_at: '2026-09-27T14:00:00Z',
      },
    ],
  })

  renderApp('/sell?sale=s1')

  const row = (await screen.findByText('500ml')).closest('tr')!
  expect(within(row).getByText('$25.00')).toBeInTheDocument()
})

// --- searching and adding a product; price, tax and stock -------------------------------------

test('a matched product shows price and stock, and adding it lines it up with tax', async () => {
  const user = userEvent.setup()
  sellStub()
  renderApp('/sell')

  await user.click(await screen.findByRole('button', { name: 'Start walk-in sale' }))
  await user.type(await screen.findByLabelText('Search products'), 'Shampoo')

  expect(await screen.findByText('Shampoo — 500ml')).toBeInTheDocument()
  expect(screen.getByText('$25.00 · 4 on hand')).toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Add' }))

  const row = (await screen.findByText('500ml')).closest('tr')!
  expect(within(row).getByText('$25.00')).toBeInTheDocument()
  expect(within(row).getByText('$1.25')).toBeInTheDocument() // 5% tax on $25
  expect(within(row).getByText('4')).toBeInTheDocument() // stock on hand
})

// --- discounts ---------------------------------------------------------------------------------

test('applying an eligible discount recomputes the total live', async () => {
  const user = userEvent.setup()
  sellStub()
  renderApp('/sell')

  await user.click(await screen.findByRole('button', { name: 'Start walk-in sale' }))
  await user.type(await screen.findByLabelText('Search products'), 'Shampoo')
  await user.click(await screen.findByRole('button', { name: 'Add' }))

  const checkbox = await screen.findByRole('checkbox', { name: /Autumn 10%/ })
  await user.click(checkbox)

  await waitFor(() => expect(checkbox).toBeChecked())
  // subtotal 2500, 10% off = 250 discount, recomputed live once the discount is applied.
  await waitFor(() => expect(screen.getByText('-$2.50')).toBeInTheDocument())
})

// --- issuing: success lands on the invoice with payment ready, a 409 reads "only N left" -------

test('issuing a sale lands on its retail invoice with the payment dialog open', async () => {
  const user = userEvent.setup()
  sellStub()
  renderApp('/sell')

  await user.click(await screen.findByRole('button', { name: 'Start walk-in sale' }))
  await user.type(await screen.findByLabelText('Search products'), 'Shampoo')
  await user.click(await screen.findByRole('button', { name: 'Add' }))
  await user.click(await screen.findByRole('button', { name: 'Issue & take payment' }))

  // The dialog opens modal, which marks the rest of the page `aria-hidden` — the same reason
  // `invoice-payments-panel.test.tsx`'s own `?pay=1` test reads the dialog, not the heading
  // behind it, as the proof that navigation landed on the retail invoice.
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByRole('heading', { name: 'Record payment' })).toBeInTheDocument()
})

test('a 409 on issue reads "only N left"', async () => {
  const user = userEvent.setup()
  sellStub({
    issueResponse: {
      status: 409,
      body: {
        detail:
          "Not enough stock for 'Shampoo — 500ml' to complete this sale: variant v1 has 1 on hand, cannot apply a delta of -2",
      },
    },
  })
  renderApp('/sell')

  await user.click(await screen.findByRole('button', { name: 'Start walk-in sale' }))
  await user.type(await screen.findByLabelText('Search products'), 'Shampoo')
  await user.click(await screen.findByRole('button', { name: 'Add' }))
  await user.click(await screen.findByRole('button', { name: 'Issue & take payment' }))

  expect(await screen.findByRole('alert')).toHaveTextContent(
    'Only 1 left of "Shampoo — 500ml".',
  )
})
