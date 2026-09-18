import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { LIVE_RESET_TOKEN, PASSWORD, renderApp, stubApi } from './harness'

const NEW_PASSWORD = 'a different long passphrase'

afterEach(() => vi.unstubAllGlobals())

async function fillNewPassword(
  user: ReturnType<typeof userEvent.setup>,
  password = NEW_PASSWORD,
  confirm = password,
) {
  await user.type(await screen.findByLabelText('New password'), password)
  await user.type(screen.getByLabelText('Confirm new password'), confirm)
}

// --- asking for a link ---------------------------------------------------------------------

test('the login screen offers a way out of a forgotten password', async () => {
  stubApi()
  const user = userEvent.setup()

  renderApp('/login')
  await user.click(await screen.findByRole('link', { name: 'Forgot password?' }))

  expect(await screen.findByRole('button', { name: 'Send reset link' })).toBeInTheDocument()
})

test('a requested link is confirmed without saying whether the account exists', async () => {
  const { calls } = stubApi()
  const user = userEvent.setup()

  renderApp('/forgot-password')
  await user.type(await screen.findByLabelText('Email'), 'nobody@cedar.example')
  await user.click(screen.getByRole('button', { name: 'Send reset link' }))

  // "If an account exists" — never "sent", which would be the membership answer the
  // backend deliberately withheld.
  expect(await screen.findByText(/If an account exists/)).toBeInTheDocument()
  expect(calls.filter((c) => c.url === '/api/auth/password-reset/request')).toEqual([
    {
      url: '/api/auth/password-reset/request',
      method: 'POST',
      contentType: 'application/json',
      body: { email: 'nobody@cedar.example' },
    },
  ])
})

// --- spending a link -----------------------------------------------------------------------

test('a live link sets the new password and sends the visitor to sign in', async () => {
  const { calls } = stubApi()
  const user = userEvent.setup()

  renderApp(`/reset-password?token=${LIVE_RESET_TOKEN}`)
  await fillNewPassword(user)
  await user.click(screen.getByRole('button', { name: 'Set new password' }))

  expect(await screen.findByText('Password updated')).toBeInTheDocument()
  expect(screen.getByText(/Every other session has been signed out/)).toBeInTheDocument()
  expect(calls.find((c) => c.url === '/api/auth/password-reset/confirm')?.body).toEqual({
    token: LIVE_RESET_TOKEN,
    new_password: NEW_PASSWORD,
  })
})

test('a spent or expired link is a dead end with a way to get another', async () => {
  stubApi()
  const user = userEvent.setup()

  renderApp('/reset-password?token=long-since-used')
  await fillNewPassword(user)
  await user.click(screen.getByRole('button', { name: 'Set new password' }))

  expect(await screen.findByText('This link has expired')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Request another link' })).toHaveAttribute(
    'href',
    '/forgot-password',
  )
  expect(screen.queryByLabelText('New password')).not.toBeInTheDocument()
})

test('a reset URL with no token at all says so instead of posting nothing', async () => {
  const { calls } = stubApi()

  renderApp('/reset-password')

  expect(await screen.findByText('This link has expired')).toBeInTheDocument()
  expect(calls.filter((c) => c.url.includes('password-reset'))).toEqual([])
})

test('the two password fields have to agree, and nothing is sent until they do', async () => {
  const { calls } = stubApi()
  const user = userEvent.setup()

  renderApp(`/reset-password?token=${LIVE_RESET_TOKEN}`)
  await fillNewPassword(user, NEW_PASSWORD, 'a different different passphrase')
  await user.click(screen.getByRole('button', { name: 'Set new password' }))

  expect(await screen.findByText('The two passwords do not match.')).toBeInTheDocument()
  expect(calls.filter((c) => c.url.includes('password-reset'))).toEqual([])
})

test('a password under the minimum is refused before a request is spent', async () => {
  const { calls } = stubApi()
  const user = userEvent.setup()

  renderApp(`/reset-password?token=${LIVE_RESET_TOKEN}`)
  await fillNewPassword(user, 'short')
  await user.click(screen.getByRole('button', { name: 'Set new password' }))

  expect(await screen.findByText('Use at least 12 characters.')).toBeInTheDocument()
  expect(calls.filter((c) => c.url.includes('password-reset'))).toEqual([])
})

test('a signed-in visitor has no business on the reset screens', async () => {
  stubApi({ signedIn: true })

  renderApp('/forgot-password')

  expect(await screen.findByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
})

// --- the forced change ---------------------------------------------------------------------

test('a flagged account reaches the change screen and nothing else', async () => {
  stubApi({ signedIn: true, mustChangePassword: true })

  renderApp('/clients')

  expect(await screen.findByText('Set a new password')).toBeInTheDocument()
  expect(screen.queryByRole('navigation', { name: 'Primary' })).not.toBeInTheDocument()
})

test('changing the password releases the account into the shell', async () => {
  stubApi({ signedIn: true, mustChangePassword: true })
  const user = userEvent.setup()

  renderApp('/')
  await user.type(await screen.findByLabelText('Current password'), PASSWORD)
  await fillNewPassword(user)
  await user.click(screen.getByRole('button', { name: 'Save new password' }))

  expect(await screen.findByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
})

test('a wrong current password is shown against that field and keeps the screen', async () => {
  stubApi({ signedIn: true, mustChangePassword: true })
  const user = userEvent.setup()

  renderApp('/')
  await user.type(await screen.findByLabelText('Current password'), 'not the password')
  await fillNewPassword(user)
  await user.click(screen.getByRole('button', { name: 'Save new password' }))

  expect(await screen.findByText('Incorrect password')).toBeInTheDocument()
  expect(screen.queryByRole('navigation', { name: 'Primary' })).not.toBeInTheDocument()
})

test('the forced screen still lets someone sign out rather than trapping them', async () => {
  stubApi({ signedIn: true, mustChangePassword: true })
  const user = userEvent.setup()

  renderApp('/')
  await user.click(await screen.findByRole('button', { name: 'Sign out' }))

  expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
})

test('an account with no flag set is not sent to the change screen', async () => {
  stubApi({ signedIn: true })

  renderApp('/')

  expect(await screen.findByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
  expect(screen.queryByText('Set a new password')).not.toBeInTheDocument()
})
