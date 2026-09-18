import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp, stubApi } from './harness'

// The switcher as the top bar shows it, plus the two answers that mean the UI's belief
// about this session is out of date. Nothing here opens the Radix menu — those tests live
// in mode-switch.test.tsx, because a menu's pointer state is per-document.

afterEach(() => vi.unstubAllGlobals())

test('a staff-only user is shown no switcher at all', async () => {
  stubApi({ signedIn: true, dualRole: false })

  renderApp('/')

  expect(await screen.findByRole('button', { name: 'Account' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Switch mode/ })).not.toBeInTheDocument()
})

test('a user holding both capabilities sees the switcher, showing Staff Mode', async () => {
  stubApi({ signedIn: true })

  renderApp('/')

  const switcher = await screen.findByRole('button', { name: /Switch mode/ })
  expect(switcher).toHaveAccessibleName('Switch mode — currently Staff Mode')
  expect(switcher).toHaveTextContent('Staff Mode')
})

test('Admin Mode shows the time left in the top bar', async () => {
  stubApi({ signedIn: true, adminWindowMs: 95_000 })

  renderApp('/')

  const switcher = await screen.findByRole('button', { name: /Switch mode/ })
  expect(switcher).toHaveTextContent('Admin Mode')
  // A minute and a half, counted down locally so it is honest between polls of /me.
  expect(switcher).toHaveTextContent(/1:3\d left/)
})

test('Admin Mode reaches the administrative endpoint', async () => {
  stubApi({ signedIn: true, adminWindowMs: 600_000 })

  renderApp('/')

  expect(await screen.findByText('Cedar Lane Clinic')).toBeInTheDocument()
})

test('Staff Mode neither shows the administrative surface nor asks for it', async () => {
  const { calls } = stubApi({ signedIn: true })

  renderApp('/')

  await screen.findByRole('button', { name: /Switch mode/ })
  expect(screen.queryByText('Business profile')).not.toBeInTheDocument()
  expect(calls.some((c) => c.url === '/api/admin/business')).toBe(false)
})

test('a 403 from an admin endpoint drops the UI to Staff Mode and says why', async () => {
  const { server } = stubApi({ signedIn: true, adminWindowMs: 600_000 })
  const user = userEvent.setup()

  renderApp('/')
  await screen.findByText('Cedar Lane Clinic')
  // The window lapses server-side; this tab has not been told yet and still believes it is
  // administering, which is exactly the state the 403 has to resolve.
  server.expireAdminWindow()

  await user.click(screen.getByRole('button', { name: 'Refresh business profile' }))

  expect(await screen.findByText('Admin Mode expired')).toBeInTheDocument()
  await waitFor(() =>
    expect(screen.getByRole('button', { name: /Switch mode/ })).toHaveTextContent('Staff Mode'),
  )
  expect(screen.queryByText('Cedar Lane Clinic')).not.toBeInTheDocument()
})

test('a 401 from the API ends the session and returns to the login screen', async () => {
  const { server } = stubApi({ signedIn: true, adminWindowMs: 600_000 })
  const user = userEvent.setup()

  renderApp('/')
  await screen.findByText('Cedar Lane Clinic')
  server.endSession()

  await user.click(screen.getByRole('button', { name: 'Refresh business profile' }))

  expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
})
