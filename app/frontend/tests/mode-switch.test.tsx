import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PASSWORD, renderApp, stubApi } from './harness'

// Its own file, and one `userEvent.setup()` for the whole of it: a Radix menu opens on
// `pointerdown` and user-event's pointer state is per-document, so a second setup in the
// same file leaves the trigger unable to open. See tests/user-menu.test.tsx.

const user = userEvent.setup()

afterEach(() => vi.unstubAllGlobals())

async function chooseMode(name: 'Staff Mode' | 'Admin Mode') {
  await user.click(await screen.findByRole('button', { name: /Switch mode/ }))
  await user.click(await screen.findByRole('menuitem', { name }))
}

test('entering Admin Mode with no window open asks for the password', async () => {
  const { calls } = stubApi({ signedIn: true })

  renderApp('/')
  await chooseMode('Admin Mode')

  const dialog = await screen.findByRole('dialog')
  expect(dialog).toHaveTextContent('Enter Admin Mode')
  // Nothing was attempted before the password was asked for.
  expect(calls.some((c) => c.url === '/api/auth/mode')).toBe(false)

  await user.type(screen.getByLabelText('Password'), 'not the password')
  await user.click(screen.getByRole('button', { name: 'Enter Admin Mode' }))

  expect(await screen.findByText('Incorrect password')).toBeInTheDocument()

  await user.clear(screen.getByLabelText('Password'))
  await user.type(screen.getByLabelText('Password'), PASSWORD)
  await user.click(screen.getByRole('button', { name: 'Enter Admin Mode' }))

  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(screen.getByRole('button', { name: /Switch mode/ })).toHaveTextContent('Admin Mode')
  expect(await screen.findByText('Cedar Lane Clinic')).toBeInTheDocument()
})

