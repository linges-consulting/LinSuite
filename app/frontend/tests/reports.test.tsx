import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * Reports (#97/#95): a nav entry shown only to an account holding `commission.view` or
 * `billing.manage`, with Commission and Package liability tabs — both empty slots for later
 * tickets. Spec #95 user story 2: "Reports appears only when I can use it, so that staff are
 * not shown a door that is not theirs."
 */

afterEach(() => vi.unstubAllGlobals())

const ACCOUNT = (capabilities: string[]) => ({
  id: 'u2',
  email: 'owner@cedar.example',
  role: 'Administrator',
  capabilities,
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
})

function reportsStub(capabilities: string[]) {
  return stubApi({
    signedIn: true,
    respond: (url) => (url === '/api/auth/me' ? Response.json(ACCOUNT(capabilities)) : undefined),
  })
}

test('Reports is offered to an account holding commission.view', async () => {
  reportsStub(['commission.view'])
  renderApp('/')

  expect(await screen.findByRole('link', { name: 'Reports' })).toBeInTheDocument()
})

test('Reports is offered to an account holding billing.manage', async () => {
  reportsStub(['billing.manage'])
  renderApp('/')

  expect(await screen.findByRole('link', { name: 'Reports' })).toBeInTheDocument()
})

test('Reports is hidden from an account holding neither', async () => {
  reportsStub(['schedule.view'])
  renderApp('/')

  await screen.findByRole('link', { name: 'Schedule' })
  await waitFor(() => expect(screen.queryByRole('link', { name: 'Reports' })).not.toBeInTheDocument())
})

test('Reports opens on Commission, with Package liability beside it', async () => {
  reportsStub(['commission.view'])
  renderApp('/reports')

  expect(
    await screen.findByRole('tab', { name: 'Commission', selected: true }),
  ).toBeInTheDocument()
  expect(await screen.findByText('The commission report is not built yet')).toBeInTheDocument()
})

test('switching to Package liability shows its empty state', async () => {
  const user = userEvent.setup()
  reportsStub(['billing.manage'])
  renderApp('/reports')

  await user.click(await screen.findByRole('tab', { name: 'Package liability' }))

  expect(
    await screen.findByText('The package-liability report is not built yet'),
  ).toBeInTheDocument()
})
