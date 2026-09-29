import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * Billing's tab shell (#97/#95): `To review` is the unchanged draft-bill list
 * (`routes/bills.tsx`'s own `BillsPage`, covered by `tests/bills.test.tsx`); `Invoices` is
 * the real tab (#99, covered in depth by `tests/invoices-tab.test.tsx`), and the invoice view
 * (#102) is covered in depth by `tests/invoice-view.test.tsx`. This file only covers the
 * shell — that both tabs exist, that switching between them works, and that the
 * `bills/invoices/:id` route reaches the invoice view rather than the bill review route.
 */

afterEach(() => vi.unstubAllGlobals())

const ACCOUNT = {
  id: 'u2',
  email: 'desk@cedar.example',
  role: 'Staff',
  capabilities: ['billing.view'],
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
}

function billingStub() {
  return stubApi({
    signedIn: true,
    respond: (url) => {
      const parsed = new URL(url, 'http://test')
      if (url === '/api/auth/me') return Response.json(ACCOUNT)
      if (url === '/api/bills') return Response.json({ bills: [] })
      if (parsed.pathname === '/api/invoices') {
        return Response.json({
          invoices: [],
          total: 0,
          from: '2026-08-30',
          to: '2026-09-29',
          timezone: 'America/Toronto',
        })
      }
      if (parsed.pathname === '/api/invoices/inv1') {
        return Response.json({ detail: 'No such invoice.' }, { status: 404 })
      }
      return undefined
    },
  })
}

test('Billing opens on To review, with Invoices beside it', async () => {
  billingStub()
  renderApp('/bills')

  expect(await screen.findByRole('tab', { name: 'To review', selected: true })).toBeInTheDocument()
  expect(screen.getByRole('tab', { name: 'Invoices' })).toBeInTheDocument()
  // The unchanged draft-bill screen, rendered inside the new tab shell.
  expect(await screen.findByText('No draft bills')).toBeInTheDocument()
})

test('switching to Invoices shows its empty state', async () => {
  const user = userEvent.setup()
  billingStub()
  renderApp('/bills')

  await user.click(await screen.findByRole('tab', { name: 'Invoices' }))

  expect(await screen.findByText('No invoices in this range')).toBeInTheDocument()
})

test('the invoice view route reaches the invoice view, not the bill review route', async () => {
  billingStub()
  renderApp('/bills/invoices/inv1')

  // A bare id under `bills/:id` would try to load a *bill* named "invoices" and 404 there
  // instead — reaching this message (from `GET /api/invoices/inv1`) proves the more specific
  // `bills/invoices/:id` route won, not `bills/:id`.
  expect(await screen.findByRole('alert')).toHaveTextContent('No such invoice.')
})
