import { screen, waitFor } from '@testing-library/react'
import type { OnboardingStatus } from '@/lib/api'
import { renderApp, stubApi } from './harness'

/**
 * "Emails are not being sent — they go to the server log" (#116, spec #113): a Home banner in
 * Admin Mode, independent of the checklist's own dismissal (spec user story 30).
 */

afterEach(() => vi.unstubAllGlobals())

const ADMIN = { signedIn: true, dualRole: true, adminWindowMs: 15 * 60_000 }
const BANNER_TEXT = 'Emails are not being sent — they go to the server log'

function status(overrides: Partial<OnboardingStatus> = {}): OnboardingStatus {
  return {
    steps: [
      { key: 'business', done: true, optional: false },
      { key: 'hours', done: true, optional: false },
      { key: 'tax', done: true, optional: false },
      { key: 'spaces', done: true, optional: false },
      { key: 'services', done: true, optional: false },
      { key: 'staff', done: true, optional: false },
      { key: 'email', done: false, optional: false },
      { key: 'branding', done: false, optional: true },
    ],
    dismissed_at: null,
    ...overrides,
  }
}

test('shows while no sender has passed a test send', async () => {
  stubApi({
    ...ADMIN,
    respond: (url) => {
      if (url === '/api/admin/onboarding') return Response.json(status())
      return undefined
    },
  })

  renderApp('/')

  expect(await screen.findByRole('alert')).toHaveTextContent(BANNER_TEXT)
})

test('is gone once the email step is done', async () => {
  stubApi({
    ...ADMIN,
    respond: (url) => {
      if (url === '/api/admin/onboarding') {
        const s = status()
        return Response.json({
          ...s,
          steps: s.steps.map((step) => (step.key === 'email' ? { ...step, done: true } : step)),
        })
      }
      return undefined
    },
  })

  renderApp('/')

  await screen.findByText('System status')
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
})

test('still shows after the checklist is dismissed', async () => {
  stubApi({
    ...ADMIN,
    respond: (url) => {
      if (url === '/api/admin/onboarding') {
        return Response.json(status({ dismissed_at: '2026-09-29T12:00:00Z' }))
      }
      return undefined
    },
  })

  renderApp('/')

  expect(await screen.findByRole('alert')).toHaveTextContent(BANNER_TEXT)
  // Dismissal only hides the checklist, never this banner.
  expect(screen.queryByText('Get your business ready')).not.toBeInTheDocument()
})

test('never renders in Staff Mode', async () => {
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
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
