import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MFA_INTERVAL_MS, PASSWORD, TOTP_CODE, renderApp, stubApi } from './harness'

/**
 * The twelve-hourly challenge on the way into Admin Mode.
 *
 * One test per file, like tests/mode-switch.test.tsx: a Radix menu opens on `pointerdown`,
 * user-event's pointer state is per-document, and a second menu interaction in the same
 * file finds the trigger dead. Vitest isolates files, which is the cheap, durable fix.
 */

const user = userEvent.setup()

afterEach(() => vi.unstubAllGlobals())

async function enterAdminMode() {
  await user.click(await screen.findByRole('button', { name: /Switch mode/ }))
  await user.click(await screen.findByRole('menuitem', { name: /Admin Mode/ }))
  return screen.findByRole('dialog')
}

test('once the interval has elapsed the dialog asks for a code as well', async () => {
  const { calls } = stubApi({
    signedIn: true,
    mfaEnrolled: true,
    verifiedAgoMs: MFA_INTERVAL_MS + 60_000,
  })

  renderApp('/')
  await enterAdminMode()

  // The dialog opens asking for the password alone: only the server knows the verification
  // has gone stale, and predicting it here would mean a second clock to be wrong about.
  expect(screen.queryByLabelText('Code from your authenticator')).not.toBeInTheDocument()
  await user.type(screen.getByLabelText('Password'), PASSWORD)
  await user.click(screen.getByRole('button', { name: 'Enter Admin Mode' }))

  // `mfa_required` comes back, and the dialog grows rather than closing — what was already
  // typed stays typed.
  const code = await screen.findByLabelText('Code from your authenticator')
  expect(screen.getByText(/about once every twelve hours/)).toBeInTheDocument()
  expect(screen.getByLabelText('Password')).toHaveValue(PASSWORD)

  await user.type(code, TOTP_CODE)
  await user.click(screen.getByRole('button', { name: 'Enter Admin Mode' }))

  await waitFor(() =>
    expect(screen.getByRole('button', { name: /Switch mode/ })).toHaveTextContent('Admin Mode'),
  )
  expect(calls.filter((c) => c.url === '/api/auth/mode').at(-1)?.body).toMatchObject({
    mode: 'admin',
    password: PASSWORD,
    totp: TOTP_CODE,
  })
})
