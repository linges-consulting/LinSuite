import { screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * The client profile's Invoices and Packages cards (#97/#95 user stories 54/65): behind
 * `billing.view`, the same front-desk capability Billing and Sell already use. Both are now
 * real cards — Invoices (#99, `tests/client-invoices.test.tsx`) and Packages (#108,
 * `tests/client-packages.test.tsx`) cover their own content in depth. This file only covers
 * whether the cards render at all.
 */

afterEach(() => vi.unstubAllGlobals())

const PROFILE = {
  customer: {
    id: 'c1',
    first_name: 'Priya',
    last_name: 'Nair',
    email: 'priya@example.com',
    phone: '4165550199',
    created_at: '2026-01-05T15:00:00Z',
    classification: 'vip' as const,
    date_of_birth: null,
    emergency_contact_name: null,
    emergency_contact_phone: null,
    emergency_contact_relationship: null,
    secondary_contact_name: null,
    secondary_contact_phone: null,
    secondary_contact_email: null,
    notes: null,
    updated_at: '2026-01-05T15:00:00Z',
    retention: { status: 'not_held' as const, expires_on: null as string | null },
  },
  timezone: 'America/Toronto',
  notification_failures: [],
  appointments: [],
}

const ACCOUNT = (capabilities: string[]) => ({
  id: 'u1',
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

function profileStub(capabilities: string[]) {
  return stubApi({
    signedIn: true,
    respond: (url) => {
      const parsed = new URL(url, 'http://test')
      if (url === '/api/auth/me') return Response.json(ACCOUNT(capabilities))
      if (parsed.pathname === '/api/customers/c1') return Response.json(PROFILE)
      if (parsed.pathname === '/api/invoices') {
        return Response.json({
          invoices: [],
          total: 0,
          from: '2026-08-30',
          to: '2026-09-29',
          timezone: 'America/Toronto',
        })
      }
      if (parsed.pathname === '/api/retail-invoices') {
        return Response.json({
          retail_invoices: [],
          total: 0,
          from: '2026-08-30',
          to: '2026-09-29',
          timezone: 'America/Toronto',
        })
      }
      if (parsed.pathname === '/api/customers/c1/package-purchases') {
        return Response.json({ purchases: [] })
      }
      return undefined
    },
  })
}

test('a client with billing.view sees Invoices and Packages cards, both empty', async () => {
  profileStub(['customers.view', 'billing.view'])
  renderApp('/clients/c1')

  expect(await screen.findByRole('heading', { name: 'Priya Nair' })).toBeInTheDocument()
  expect(await screen.findByText('No invoices yet')).toBeInTheDocument()
  expect(await screen.findByText('No packages yet')).toBeInTheDocument()
})

test('without billing.view neither card renders', async () => {
  profileStub(['customers.view'])
  renderApp('/clients/c1')

  await screen.findByRole('heading', { name: 'Priya Nair' })
  await waitFor(() => expect(screen.queryByText('No invoices yet')).not.toBeInTheDocument())
  expect(screen.queryByText('No packages yet')).not.toBeInTheDocument()
})
