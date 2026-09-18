import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp, stubApi } from './harness'

// Its own file on purpose: a Radix menu opens on `pointerdown`, and user-event's pointer
// state is per-document, so a second `userEvent.setup()` in the same file leaves the
// trigger unable to open. Vitest isolates files, which is the cheap, durable fix.

afterEach(() => vi.unstubAllGlobals())

test('the top bar names the signed-in account and logs it out', async () => {
  const calls = stubApi({ signedIn: true })
  const user = userEvent.setup()

  renderApp('/')
  await user.click(await screen.findByRole('button', { name: 'Account' }))

  expect(screen.getByRole('menu')).toHaveTextContent('owner@cedar.example')

  await user.click(screen.getByRole('menuitem', { name: 'Log out' }))

  expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  expect(calls.some((c) => c.url === '/api/auth/logout' && c.method === 'POST')).toBe(true)
})
