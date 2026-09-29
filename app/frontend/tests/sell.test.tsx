import { screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * Sell (#97/#95): replaces the empty Catalog placeholder in the main nav, gated the same as
 * Billing (`billing.view`). Nothing to click yet — its own ticket is the slot in
 * `routes/sell.tsx` — so this only covers nav gating and the empty state.
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

function sellStub(capabilities: string[]) {
  return stubApi({
    signedIn: true,
    respond: (url) => (url === '/api/auth/me' ? Response.json(ACCOUNT(capabilities)) : undefined),
  })
}

test('the Sell nav entry is offered when billing.view is held', async () => {
  sellStub(['billing.view'])
  renderApp('/')

  expect(await screen.findByRole('link', { name: 'Sell' })).toBeInTheDocument()
})

test('without billing.view there is no Sell nav entry', async () => {
  sellStub(['schedule.view'])
  renderApp('/')

  await screen.findByRole('link', { name: 'Schedule' })
  await waitFor(() => expect(screen.queryByRole('link', { name: 'Sell' })).not.toBeInTheDocument())
})

test('the Sell route renders its empty state', async () => {
  sellStub(['billing.view'])
  renderApp('/sell')

  expect(await screen.findByText('Sell is not built yet')).toBeInTheDocument()
})
