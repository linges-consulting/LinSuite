import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { toast } from 'sonner'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * Retail returns (#105, spec #95 story 50): the *Return items* dialog on a retail invoice's
 * view. Admin Mode (`billing.manage`) gating is already proven by `invoice-view.test.tsx`
 * ("returns render only in Admin Mode with billing.manage") — this file covers the dialog's
 * own behaviour: quantities capped at what remains returnable, the refund shown and sent,
 * and a clear server refusal.
 */

afterEach(() => vi.unstubAllGlobals())
beforeEach(() => toast.dismiss())

const ACCOUNT = {
  id: 'u1',
  email: 'admin@cedar.example',
  role: 'Administrator',
  capabilities: ['billing.view', 'billing.manage'],
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

function retailInvoice(overrides: Record<string, unknown> = {}) {
  return {
    id: 'rinv1',
    invoice_number: 7,
    retail_sale_id: 'rs1',
    customer_id: null,
    customer_name: null,
    status: 'issued' as const,
    subtotal_cents: 4500,
    discount_total_cents: 0,
    tax_total_cents: 500,
    tax_totals_by_component: { GST: 500 },
    grand_total_cents: 5000,
    sold_by_staff_id: 'st1',
    payment_collector_staff_id: null,
    issued_at: '2026-09-18T15:00:00Z',
    issued_by: 'u1',
    lines: [
      {
        id: 'rl1',
        variant_id: 'v1',
        variant_name: 'Shampoo 250ml',
        quantity: 3,
        unit_price_cents: 1500,
        discount_cents: 0,
        tax_cents: 300,
        tax_convention: 'exclusive' as const,
        line_total_cents: 4800,
        discounts: [],
        taxes: [{ component_code: 'GST', rate_bp: 500, amount_cents: 300 }],
        staff_id: 'st1',
        returned_quantity: 1,
      },
      {
        id: 'rl2',
        variant_id: 'v2',
        variant_name: 'Conditioner 250ml',
        quantity: 2,
        unit_price_cents: 100,
        discount_cents: 0,
        tax_cents: 20,
        tax_convention: 'exclusive' as const,
        line_total_cents: 220,
        discounts: [],
        taxes: [{ component_code: 'GST', rate_bp: 500, amount_cents: 20 }],
        staff_id: 'st1',
        returned_quantity: 2,
      },
    ],
    replaces_invoice_id: null,
    replaced_by_invoice_id: null,
    cancelled_at: null,
    cancel_reason: null,
    outstanding_cents: 0,
    pending_insurer_cents: 0,
    client_outstanding_cents: 0,
    checkout_complete: true,
    refunded_cents: 0,
    prepaid_cents: 0,
    held_credit_cents: 0,
    ...overrides,
  }
}

function stub(opts: {
  invoice?: ReturnType<typeof retailInvoice>
  returnResponse?: (body: any) => Response
} = {}) {
  let invoice = opts.invoice ?? retailInvoice()
  const calls: { url: string; body: any }[] = []
  const api = stubApi({
    signedIn: true,
    respond: (url: string, body: any) => {
      const parsed = new URL(url, 'http://test')
      if (url === '/api/auth/me') return Response.json(ACCOUNT)
      // The payments panel (#103) renders on the same page; an empty ledger is enough here.
      if (parsed.pathname === `/api/retail-invoices/${invoice.id}/payments`) {
        return Response.json({ payments: [], transfers: [] })
      }
      if (parsed.pathname === `/api/retail-invoices/${invoice.id}/refunds`) {
        return Response.json({ refunds: [] })
      }
      if (parsed.pathname === `/api/retail-invoices/${invoice.id}/balance-exceptions`) {
        return Response.json({ exceptions: [] })
      }
      if (parsed.pathname === `/api/retail-invoices/${invoice.id}` && !parsed.pathname.endsWith('/returns')) {
        return Response.json(invoice)
      }
      if (parsed.pathname === `/api/retail-invoices/${invoice.id}/returns`) {
        calls.push({ url, body })
        if (opts.returnResponse) return opts.returnResponse(body)
        invoice = {
          ...invoice,
          lines: invoice.lines.map((line: any) => {
            const match = body.lines.find((l: any) => l.retail_invoice_line_id === line.id)
            return match ? { ...line, returned_quantity: line.returned_quantity + match.quantity } : line
          }),
        }
        return Response.json(
          {
            id: 'ret1',
            retail_invoice_id: invoice.id,
            reason: body.reason,
            refund_id: body.refund_cents ? 'rf1' : null,
            refund_cents: body.refund_cents ?? null,
            returned_by: 'u1',
            returned_at: new Date().toISOString(),
            lines: body.lines.map((l: any) => ({
              id: 'rtl1',
              retail_invoice_line_id: l.retail_invoice_line_id,
              quantity: l.quantity,
              restocked: l.restock,
            })),
          },
          { status: 201 },
        )
      }
      return undefined
    },
  })
  return { ...api, calls }
}

async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  renderApp('/bills/invoices/rinv1?kind=retail')
  await screen.findByRole('heading', { name: 'Retail invoice #7' })
  await user.click(screen.getByRole('button', { name: 'Return items' }))
  return screen.getByRole('dialog')
}

test('only lines with remaining quantity are offered, each capped at what remains', async () => {
  const user = userEvent.setup()
  stub()
  const dialog = await openDialog(user)

  // rl1: sold 3, returned 1 -> 2 left. rl2: sold 2, returned 2 -> fully returned, absent.
  expect(within(dialog).getByText('Shampoo 250ml')).toBeInTheDocument()
  expect(within(dialog).queryByText('Conditioner 250ml')).not.toBeInTheDocument()
  expect(within(dialog).getByText('Sold 3 · 2 left to return')).toBeInTheDocument()

  const qty = within(dialog).getByLabelText('Qty') as HTMLInputElement
  expect(qty).toHaveAttribute('max', '2')
})

test('typing past what remains is clamped to the remaining quantity', async () => {
  const user = userEvent.setup()
  stub()
  const dialog = await openDialog(user)

  const qty = within(dialog).getByLabelText('Qty') as HTMLInputElement
  await user.clear(qty)
  await user.type(qty, '99')

  expect(qty).toHaveValue(2)
})

test('submitting sends the chosen line, quantity, restock and refund', async () => {
  const user = userEvent.setup()
  const server = stub()
  const dialog = await openDialog(user)

  const qty = within(dialog).getByLabelText('Qty')
  await user.clear(qty)
  await user.type(qty, '2')
  await user.type(within(dialog).getByLabelText('Reason'), 'Client changed mind')
  await user.type(within(dialog).getByLabelText('Refund'), '30')
  await user.click(within(dialog).getByRole('button', { name: 'Return items' }))

  await waitFor(() => expect(server.calls).toHaveLength(1))
  expect(server.calls[0].body).toEqual({
    reason: 'Client changed mind',
    lines: [{ retail_invoice_line_id: 'rl1', quantity: 2, restock: true }],
    refund_cents: 3000,
  })
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
})

test('unchecking restock sends restock: false', async () => {
  const user = userEvent.setup()
  const server = stub()
  const dialog = await openDialog(user)

  await user.type(within(dialog).getByLabelText('Qty'), '1')
  await user.click(within(dialog).getByRole('checkbox', { name: 'Restock' }))
  await user.type(within(dialog).getByLabelText('Reason'), 'Opened, not resellable')
  await user.click(within(dialog).getByRole('button', { name: 'Return items' }))

  await waitFor(() => expect(server.calls).toHaveLength(1))
  expect(server.calls[0].body.lines[0]).toMatchObject({ quantity: 1, restock: false })
  expect(server.calls[0].body.refund_cents).toBeUndefined()
})

test('the submit button is disabled until a line and a reason are given', async () => {
  const user = userEvent.setup()
  stub()
  const dialog = await openDialog(user)

  expect(within(dialog).getByRole('button', { name: 'Return items' })).toBeDisabled()

  await user.type(within(dialog).getByLabelText('Qty'), '1')
  expect(within(dialog).getByRole('button', { name: 'Return items' })).toBeDisabled()

  await user.type(within(dialog).getByLabelText('Reason'), 'Wrong size')
  expect(within(dialog).getByRole('button', { name: 'Return items' })).toBeEnabled()
})

test('a server refusal is shown clearly', async () => {
  const user = userEvent.setup()
  stub({
    returnResponse: () =>
      Response.json({ detail: 'Only 1 of this line can still be returned.' }, { status: 422 }),
  })
  const dialog = await openDialog(user)

  await user.type(within(dialog).getByLabelText('Qty'), '2')
  await user.type(within(dialog).getByLabelText('Reason'), 'Client changed mind')
  await user.click(within(dialog).getByRole('button', { name: 'Return items' }))

  expect(await within(dialog).findByRole('alert')).toHaveTextContent(
    'Only 1 of this line can still be returned.',
  )
})
