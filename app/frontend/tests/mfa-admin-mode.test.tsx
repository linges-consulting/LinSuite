import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PASSWORD, renderApp, stubApi } from './harness'

/**
 * The code step the Admin Mode dialog grows, and the case that matters more: when it does
 * not. PRD §1 asks for a challenge at sign-in and once per twelve hours — a dialog that
 * asked on every switch would make the fifteen-minute window unusable.
 *
 * Its own file, and one `userEvent.setup()` with the menu click as the first interaction,
 * for the reason tests/mode-switch.test.tsx gives: a Radix menu opens on `pointerdown` and
 * user-event's pointer state is per-document.
 */

const user = userEvent.setup()

afterEach(() => vi.unstubAllGlobals())

async function enterAdminMode() {
  await user.click(await screen.findByRole('button', { name: /Switch mode/ }))
  await user.click(await screen.findByRole('menuitem', { name: /Admin Mode/ }))
  return screen.findByRole('dialog')
}

test('a recent verification is not challenged again — the password is enough', async () => {
  // Verified a minute ago, the way a session is right after the login challenge.
  const { calls } = stubApi({ signedIn: true, mfaEnrolled: true, verifiedAgoMs: 60_000 })

  renderApp('/')
  await enterAdminMode()

  expect(screen.queryByLabelText('Code from your authenticator')).not.toBeInTheDocument()

  await user.type(screen.getByLabelText('Password'), PASSWORD)
  await user.click(screen.getByRole('button', { name: 'Enter Admin Mode' }))

  await waitFor(() =>
    expect(screen.getByRole('button', { name: /Switch mode/ })).toHaveTextContent('Admin Mode'),
  )
  expect(calls.filter((c) => c.url === '/api/auth/mode').at(-1)?.body).toMatchObject({
    mode: 'admin',
    password: PASSWORD,
  })
})
