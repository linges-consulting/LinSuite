import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { OnboardingStatus } from '@/lib/api'
import { renderApp, stubApi } from './harness'

/**
 * "Get your business ready" (#116, spec #113): the checklist on Home. Admin Mode, `admin`
 * capability only — and once its `dismissed_at` is set, it is gone for good (the separate email
 * banner, covered in `onboarding-email-banner.test.tsx`, is the one thing dismissal never hides).
 */

afterEach(() => vi.unstubAllGlobals())

const ADMIN = { signedIn: true, dualRole: true, adminWindowMs: 15 * 60_000 }

function status(overrides: Partial<OnboardingStatus> = {}): OnboardingStatus {
  return {
    steps: [
      { key: 'business', done: true, optional: false },
      { key: 'hours', done: true, optional: false },
      { key: 'tax', done: false, optional: false },
      { key: 'spaces', done: false, optional: false },
      { key: 'services', done: false, optional: false },
      { key: 'staff', done: true, optional: false },
      { key: 'email', done: false, optional: false },
      { key: 'branding', done: false, optional: true },
    ],
    dismissed_at: null,
    ...overrides,
  }
}

test('shows progress, an Optional label on branding, and a link to each step\'s focused page', async () => {
  stubApi({
    ...ADMIN,
    respond: (url) => {
      if (url === '/api/admin/onboarding') return Response.json(status())
      return undefined
    },
  })

  renderApp('/')

  expect(await screen.findByText('Get your business ready')).toBeInTheDocument()
  expect(screen.getByText('3 of 8 steps done')).toBeInTheDocument()

  const brandingRow = screen.getByText('Branding').closest('li')!
  expect(within(brandingRow).getByText('Optional')).toBeInTheDocument()
  // Every other step carries no such label.
  expect(within(screen.getByText('Tax').closest('li')!).queryByText('Optional')).not.toBeInTheDocument()

  // #117: each step opens its own focused page rather than a Settings tab.
  expect(screen.getByRole('link', { name: /Tax/ })).toHaveAttribute('href', '/setup-checklist/tax')
  expect(screen.getByRole('link', { name: /Email sending/ })).toHaveAttribute(
    'href',
    '/setup-checklist/email',
  )
  expect(screen.getByRole('link', { name: /Opening hours/ })).toHaveAttribute(
    'href',
    '/setup-checklist/hours',
  )
})

test('Dismiss removes the checklist from Home', async () => {
  const user = userEvent.setup()
  let dismissedAt: string | null = null
  stubApi({
    ...ADMIN,
    respond: (url) => {
      if (url === '/api/admin/onboarding') return Response.json(status({ dismissed_at: dismissedAt }))
      if (url === '/api/admin/onboarding/dismiss') {
        dismissedAt = '2026-09-29T12:00:00Z'
        return Response.json(status({ dismissed_at: dismissedAt }))
      }
      return undefined
    },
  })

  renderApp('/')

  await user.click(await screen.findByRole('button', { name: 'Dismiss' }))

  await waitFor(() => expect(screen.queryByText('Get your business ready')).not.toBeInTheDocument())
})

test('never renders for an account without the admin capability', async () => {
  let asked = false
  stubApi({
    signedIn: true,
    dualRole: false,
    respond: (url) => {
      if (url === '/api/admin/onboarding') {
        asked = true
        return Response.json(status())
      }
      return undefined
    },
  })

  renderApp('/')

  await screen.findByText('System status')
  expect(screen.queryByText('Get your business ready')).not.toBeInTheDocument()
  expect(asked).toBe(false)
})

test('never renders in Staff Mode, even for an account that holds admin', async () => {
  stubApi({
    signedIn: true,
    dualRole: true,
    adminWindowMs: 0,
    respond: (url) => {
      if (url === '/api/admin/onboarding') return Response.json(status())
      return undefined
    },
  })

  renderApp('/')

  await screen.findByText('System status')
  expect(screen.queryByText('Get your business ready')).not.toBeInTheDocument()
})
