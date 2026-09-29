import { screen, waitFor } from '@testing-library/react'
import { renderApp, stubApi } from './harness'

afterEach(() => vi.unstubAllGlobals())

/**
 * Settings is the RBAC panel, and every request behind it is refused without an
 * administrative capability. Offering the link to somebody holding none of them leads only
 * to an error paragraph, which reads as the app being broken rather than as a door that was
 * never theirs.
 *
 * Hiding is a courtesy and never the enforcement — `Requires` on each route is what refuses,
 * and the harness still answers 403 to a staff-only account that types the URL.
 */

test('an administrator is offered Settings', async () => {
  stubApi({ signedIn: true, dualRole: true })

  renderApp('/')

  expect(await screen.findByRole('link', { name: 'Settings' })).toBeInTheDocument()
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
// `catalog.manage`.
test.each(['billing.manage', 'inventory.receive', 'inventory.adjust'])(
  'an account holding only %s is still offered Settings',
  async (capability) => {
    stubApi({
      signedIn: true,
      dualRole: false,
      respond: (url) =>
        url === '/api/auth/me'
          ? Response.json({
              id: 'u1',
              email: 'owner@cedar.example',
              role: 'Staff',
              capabilities: ['schedule.view', capability],
              mode: 'staff',
              can_switch_modes: true,
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
          : undefined,
    })

    renderApp('/')

    expect(await screen.findByRole('link', { name: 'Settings' })).toBeInTheDocument()
  },
)
