import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp, stubApi } from './harness'

// Its own file: user-event's pointer state is per-document and survives a test's cleanup,
// so a Radix menu opened in a second test of the same file never opens. See
// tests/user-menu.test.tsx.

afterEach(() => vi.unstubAllGlobals())

test('switching to Staff Mode and back inside the window asks for nothing', async () => {
  const { calls } = stubApi({ signedIn: true, adminWindowMs: 600_000 })
  const user = userEvent.setup()

  renderApp('/')
  const switcher = await screen.findByRole('button', { name: /Switch mode/ })
  await user.click(switcher)
  await user.click(await screen.findByRole('menuitem', { name: 'Staff Mode' }))

  await waitFor(() => expect(switcher).toHaveTextContent('Staff Mode'))

  await user.click(switcher)
  await user.click(await screen.findByRole('menuitem', { name: 'Admin Mode' }))

  await waitFor(() => expect(switcher).toHaveTextContent('Admin Mode'))
  // No password was asked for, and none was sent: the grant from before is still live.
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(calls.filter((c) => c.url === '/api/auth/mode').map((c) => c.body)).toEqual([
    { mode: 'staff' },
    { mode: 'admin' },
  ])
})
