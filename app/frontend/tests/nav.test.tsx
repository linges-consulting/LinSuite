import { screen, waitFor } from '@testing-library/react'
import { ADMIN_WINDOW_MS, renderApp, stubApi } from './harness'

afterEach(() => vi.unstubAllGlobals())

/**
 * Settings is the RBAC panel, and every request behind it is refused without an
 * administrative capability. Offering the link to somebody holding none of them leads only
 * to an error paragraph, which reads as the app being broken rather than as a door that was
 * never theirs.
 *
 * Hiding is a courtesy and never the enforcement — `Requires` on each route is what refuses,
 * and the harness still answers 403 to a staff-only account that types the URL.
 *
 * #114 (spec #113 Staff Mode section): every one of Settings' and Reports' `anyOf`
 * capabilities is administrative, so holding one is no longer enough on its own — the
 * session also has to be in Admin Mode right now, the same rule `useCan` already applies to
 * a single capability.
 */

test('an administrator in Admin Mode is offered Settings', async () => {
  stubApi({ signedIn: true, dualRole: true, adminWindowMs: ADMIN_WINDOW_MS })

  renderApp('/')

  expect(await screen.findByRole('link', { name: 'Settings' })).toBeInTheDocument()
})

test('an administrator in Staff Mode is not offered Settings', async () => {
  // No live admin window: `stubApi` starts this session in Staff Mode.
  stubApi({ signedIn: true, dualRole: true })

  renderApp('/')

  // The rest of the nav is untouched — this hides one entry, not the sidebar.
  expect(await screen.findByRole('link', { name: 'Schedule' })).toBeInTheDocument()
  await waitFor(() =>
    expect(screen.queryByRole('link', { name: 'Settings' })).not.toBeInTheDocument(),
  )
})

test('an account with no administrative capability is not offered Settings', async () => {
  // `dualRole: false` is the staff-only session: `schedule.view` and `customers.view`, and
  // none of the four capabilities the registry marks administrative.
  stubApi({ signedIn: true, dualRole: false })

  renderApp('/')

  // The rest of the nav is untouched — this hides one entry, not the sidebar.
  expect(await screen.findByRole('link', { name: 'Schedule' })).toBeInTheDocument()
  await waitFor(() =>
    expect(screen.queryByRole('link', { name: 'Settings' })).not.toBeInTheDocument(),
  )
})

// #95: a receive- or adjust-only lead, or a packages-only holder, needs the same door — each
// opens a real Settings sub-screen (Products, Products, Packages respectively) without
// `catalog.manage`. #114: all three are administrative too, so this account needs Admin Mode
// as well as the capability.
function accountWith(capability: string, mode: 'staff' | 'admin') {
  return {
    id: 'u1',
    email: 'owner@cedar.example',
    role: 'Staff',
    capabilities: ['schedule.view', capability],
    mode,
    can_switch_modes: true,
    admin_grant_expires_at: mode === 'admin' ? new Date(Date.now() + ADMIN_WINDOW_MS).toISOString() : null,
    admin_hard_limit_at: mode === 'admin' ? new Date(Date.now() + ADMIN_WINDOW_MS).toISOString() : null,
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
}

test.each(['billing.manage', 'inventory.receive', 'inventory.adjust'])(
  'an account holding only %s in Admin Mode is still offered Settings',
  async (capability) => {
    stubApi({
      signedIn: true,
      dualRole: false,
      respond: (url) =>
        url === '/api/auth/me' ? Response.json(accountWith(capability, 'admin')) : undefined,
    })

    renderApp('/')

    expect(await screen.findByRole('link', { name: 'Settings' })).toBeInTheDocument()
  },
)

test.each(['billing.manage', 'inventory.receive', 'inventory.adjust'])(
  'an account holding only %s in Staff Mode is not offered Settings',
  async (capability) => {
    stubApi({
      signedIn: true,
      dualRole: false,
      respond: (url) =>
        url === '/api/auth/me' ? Response.json(accountWith(capability, 'staff')) : undefined,
    })

    renderApp('/')

    await screen.findByRole('link', { name: 'Schedule' })
    await waitFor(() =>
      expect(screen.queryByRole('link', { name: 'Settings' })).not.toBeInTheDocument(),
    )
  },
)
