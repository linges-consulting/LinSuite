import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp, stubApi, tooManyRequests } from './harness'

// Its own file: user-event's pointer state is per-document and survives a test's cleanup, so
// a Radix menu opened in a second test of the same file never opens. See tests/mode-window.

afterEach(() => vi.unstubAllGlobals())

test('the Admin Mode dialog shows a lockout instead of asking again', async () => {
  stubApi({
    signedIn: true,
    respond: (url, body) =>
      url === '/api/auth/mode' && body?.password
        ? tooManyRequests(30 * 60, { locked: true })
        : undefined,
  })
  const user = userEvent.setup()

  renderApp('/')
  await user.click(await screen.findByRole('button', { name: /Switch mode/ }))
  await user.click(await screen.findByRole('menuitem', { name: 'Admin Mode' }))
  await user.type(await screen.findByLabelText('Password'), 'not the password')
  await user.click(screen.getByRole('button', { name: 'Enter Admin Mode' }))

  expect(await screen.findByRole('alert')).toHaveTextContent(/temporarily locked until/)
  // Re-authenticating is the one thing that cannot help, so the button says so.
  expect(screen.getByRole('button', { name: 'Enter Admin Mode' })).toBeDisabled()
})
