import { act, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp, stubApi, tooManyRequests } from './harness'

/**
 * What a 429 looks like to somebody using the product.
 *
 * The server refuses an attempt that came too soon rather than holding the request open, so
 * the wait is the browser's to show — and a screen that answered "incorrect password" to a
 * locked account would send a user to retype a password that was never the problem.
 */

beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }))
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

const typing = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime })

async function tick(ms: number) {
  await act(async () => {
    vi.advanceTimersByTime(ms)
  })
}

async function signIn(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText('Email'), 'owner@cedar.example')
  await user.type(screen.getByLabelText('Password'), 'not the password')
  await user.click(screen.getByRole('button', { name: 'Sign in' }))
}

test('a delayed sign-in counts the wait down and re-enables itself', async () => {
  stubApi({ respond: (url) => (url === '/api/auth/login' ? tooManyRequests(3) : undefined) })
  const user = typing()

  renderApp('/login')
  await signIn(user)

  const button = await screen.findByRole('button', { name: 'Sign in' })
  expect(await screen.findByRole('alert')).toHaveTextContent('Too many attempts — try again in 3 s.')
  expect(button).toBeDisabled()

  await tick(1000)
  expect(screen.getByRole('alert')).toHaveTextContent('try again in 2 s.')

  await tick(2000)
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(button).toBeEnabled()
})

test('a locked account is named as locked, with the time it reopens', async () => {
  const fifteenMinutes = 15 * 60
  stubApi({
    respond: (url) =>
      url === '/api/auth/login' ? tooManyRequests(fifteenMinutes, { locked: true }) : undefined,
  })
  const user = typing()

  renderApp('/login')
  await signIn(user)

  const reopens = new Date(Date.now() + fifteenMinutes * 1000).toLocaleTimeString([], {
    hour: 'numeric',
    minute: '2-digit',
  })
  expect(await screen.findByRole('alert')).toHaveTextContent(
    `This account is temporarily locked until ${reopens}.`,
  )
  // No countdown for a quarter of an hour, and no invitation to try again meanwhile.
  expect(screen.getByRole('button', { name: 'Sign in' })).toBeDisabled()
  expect(screen.queryByText('Incorrect email or password')).not.toBeInTheDocument()
})

test('the forgotten-password form says when it will take another request', async () => {
  stubApi({
    respond: (url) =>
      url === '/api/auth/password-reset/request' ? tooManyRequests(5) : undefined,
  })
  const user = typing()

  renderApp('/forgot-password')
  await user.type(await screen.findByLabelText('Email'), 'owner@cedar.example')
  await user.click(screen.getByRole('button', { name: 'Send reset link' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('try again in 5 s.')
  expect(screen.getByRole('button', { name: 'Send reset link' })).toBeDisabled()
})

test('the forced change screen is throttled like a login', async () => {
  stubApi({
    signedIn: true,
    mustChangePassword: true,
    respond: (url) => (url === '/api/auth/password/change' ? tooManyRequests(4) : undefined),
  })
  const user = typing()

  renderApp('/')
  await user.type(await screen.findByLabelText('Current password'), 'not the password')
  await user.type(screen.getByLabelText('New password'), 'a different long passphrase')
  await user.type(screen.getByLabelText('Confirm new password'), 'a different long passphrase')
  await user.click(screen.getByRole('button', { name: 'Save new password' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('try again in 4 s.')
  expect(screen.getByRole('button', { name: 'Save new password' })).toBeDisabled()

  await tick(4000)
  expect(screen.getByRole('button', { name: 'Save new password' })).toBeEnabled()
})
