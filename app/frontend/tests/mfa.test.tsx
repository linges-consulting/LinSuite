import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {
  EMAILED_CODE,
  FRESH_CODES,
  RECOVERY_CODE,
  TOTP_CODE,
  renderApp,
  stubApi,
} from './harness'

/**
 * The second factor, as the browser meets it: the two gates that route, the verify screen,
 * the enrolment flow, and the code step the Admin Mode dialog grows when the server asks.
 *
 * Nothing here checks a code itself — that is the server's job and `test_mfa.py`'s subject.
 * What is under test is that this app routes on what `/me` says, sends what the endpoints
 * expect, and never shows the recovery codes twice.
 */

afterEach(() => vi.unstubAllGlobals())

// --- the pending gate ---------------------------------------------------------------------

test('an enrolled account lands on the verify screen and nowhere else', async () => {
  stubApi({ signedIn: true, mfaEnrolled: true })

  renderApp('/settings')

  // Signed in, and still not in the application: the session owes a code.
  expect(await screen.findByText('Enter your code')).toBeInTheDocument()
  expect(screen.queryByRole('navigation', { name: 'Primary' })).not.toBeInTheDocument()
})

test('a valid code lets the session into the shell', async () => {
  stubApi({ signedIn: true, mfaEnrolled: true })
  const user = userEvent.setup()

  renderApp('/')
  await screen.findByText('Enter your code')
  await user.type(screen.getByLabelText('Code'), TOTP_CODE)
  await user.click(screen.getByRole('button', { name: 'Continue' }))

  expect(await screen.findByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
})

test('a recovery code works on the same field', async () => {
  stubApi({ signedIn: true, mfaEnrolled: true })
  const user = userEvent.setup()

  renderApp('/')
  await screen.findByText('Enter your code')
  await user.type(screen.getByLabelText('Code'), RECOVERY_CODE)
  await user.click(screen.getByRole('button', { name: 'Continue' }))

  expect(await screen.findByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
})

test('a wrong code says so and leaves the screen where it is', async () => {
  stubApi({ signedIn: true, mfaEnrolled: true })
  const user = userEvent.setup()

  renderApp('/')
  await screen.findByText('Enter your code')
  await user.type(screen.getByLabelText('Code'), '000000')
  await user.click(screen.getByRole('button', { name: 'Continue' }))

  expect(await screen.findByText(/not right/)).toBeInTheDocument()
  expect(screen.getByText('Enter your code')).toBeInTheDocument()
})

test('the emailed fallback is offered, and its tradeoff is stated on the screen', async () => {
  const { calls } = stubApi({ signedIn: true, mfaEnrolled: true, emailOtpAllowed: true })
  const user = userEvent.setup()

  renderApp('/')
  await screen.findByText('Enter your code')
  expect(screen.getByText(/weaker than an authenticator app/)).toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Email me a code instead' }))
  await screen.findByRole('button', { name: /Code sent/ })
  await user.type(screen.getByLabelText('Code'), EMAILED_CODE)
  await user.click(screen.getByRole('button', { name: 'Continue' }))

  expect(await screen.findByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
  expect(calls.some((c) => c.url === '/api/auth/mfa/email-otp/request')).toBe(true)
})

// --- the enrolment gate -------------------------------------------------------------------

test('the policy leaves an unenrolled administrator on the enrolment screen', async () => {
  stubApi({ signedIn: true, policyOn: true })

  renderApp('/settings')

  expect(await screen.findByText('Set up your second factor')).toBeInTheDocument()
  expect(screen.getByText(/requires a second factor/)).toBeInTheDocument()
  expect(screen.queryByRole('navigation', { name: 'Primary' })).not.toBeInTheDocument()
})

test('with the policy off nobody is pushed into enrolment', async () => {
  stubApi({ signedIn: true, policyOn: false })

  renderApp('/')

  expect(await screen.findByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
})

test('the policy does not reach a staff account that cannot administer', async () => {
  stubApi({ signedIn: true, policyOn: true, dualRole: false })

  renderApp('/')

  expect(await screen.findByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
})

// --- enrolling ------------------------------------------------------------------------------

// The one test here that waits on two chained round trips, which is worth more than
// vitest's default five seconds when seventeen jsdom workers are competing for the box.
test('enrolment shows a QR code, a typeable key, and the codes exactly once', async () => {
  stubApi({ signedIn: true, policyOn: true })
  const user = userEvent.setup()

  renderApp('/')
  await screen.findByText('Set up your second factor')
  expect(
    await screen.findByRole('img', { name: 'Scan this with your authenticator app' }),
  ).toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: "Can't scan it?" }))
  expect(screen.getByText('JBSWY3DPEHPK3PXP')).toBeInTheDocument()

  await user.type(screen.getByLabelText('Code from the app'), TOTP_CODE)
  await user.click(screen.getByRole('button', { name: 'Confirm' }))

  const codes = await screen.findByRole('list', { name: 'Recovery codes' })
  for (const code of FRESH_CODES) expect(within(codes).getByText(code)).toBeInTheDocument()

  // Leaving is deliberate: a redirect on success would close the one window they exist in.
  // It waits on a re-read of the session before navigating — two round trips, so the
  // default one-second window is not the right measure of "did it get there", and the whole
  // suite runs seventeen jsdom workers at once.
  await user.click(screen.getByRole('button', { name: 'I have saved them' }))
  expect(
    await screen.findByRole('navigation', { name: 'Primary' }, { timeout: 8000 }),
  ).toBeInTheDocument()
  expect(screen.queryByRole('list', { name: 'Recovery codes' })).not.toBeInTheDocument()
}, 15_000)

test('a wrong confirmation code does not enrol the account', async () => {
  stubApi({ signedIn: true, policyOn: true })
  const user = userEvent.setup()

  renderApp('/')
  await screen.findByLabelText('Code from the app')
  await user.type(screen.getByLabelText('Code from the app'), '000000')
  await user.click(screen.getByRole('button', { name: 'Confirm' }))

  expect(await screen.findByText(/not right/)).toBeInTheDocument()
  expect(screen.queryByRole('list', { name: 'Recovery codes' })).not.toBeInTheDocument()
})

test('a business that allows emailed codes offers the choice, with the warning', async () => {
  stubApi({ signedIn: true, policyOn: true, emailOtpAllowed: true })
  const user = userEvent.setup()

  renderApp('/')
  await screen.findByRole('button', { name: 'Use an authenticator app' })
  expect(screen.getByText(/weaker than an authenticator app/)).toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Email me a code each time' }))
  await user.type(await screen.findByLabelText('Code from your email'), EMAILED_CODE)
  await user.click(screen.getByRole('button', { name: 'Confirm' }))

  expect(await screen.findByRole('list', { name: 'Recovery codes' })).toBeInTheDocument()
})

// --- the account's own Security page ------------------------------------------------------

test('the Security page counts the remaining codes and can replace them', async () => {
  // Verified a minute ago, so the session is past both gates and the page is reachable.
  stubApi({ signedIn: true, mfaEnrolled: true, verifiedAgoMs: 60_000 })
  const user = userEvent.setup()

  renderApp('/security')

  expect(await screen.findByText('Two-factor authentication')).toBeInTheDocument()
  expect(screen.getByText(/1 of your codes are still unused/)).toBeInTheDocument()
  // The codes themselves are not on this page and cannot be: only their digests survived
  // the one time they were shown.
  expect(screen.queryByRole('list', { name: 'Recovery codes' })).not.toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Generate new codes' }))

  const list = await screen.findByRole('list', { name: 'Recovery codes' })
  expect(within(list).getByText(FRESH_CODES[0])).toBeInTheDocument()
})

// --- the gates in the order the server will actually answer in ---------------------------

test('a session owing both a code and a password change verifies first', async () => {
  // The change endpoint refuses a pending session, so the change screen would be a form
  // that cannot be submitted. The order here is which screen's own calls will be answered,
  // not the order the server happens to check in.
  stubApi({ signedIn: true, mfaEnrolled: true, mustChangePassword: true })
  const user = userEvent.setup()

  renderApp('/')

  expect(await screen.findByText('Enter your code')).toBeInTheDocument()
  expect(screen.queryByText('Set a new password')).not.toBeInTheDocument()

  await user.type(screen.getByLabelText('Code'), TOTP_CODE)
  await user.click(screen.getByRole('button', { name: 'Continue' }))

  // Verified, and the change is still owed.
  expect(await screen.findByText('Set a new password')).toBeInTheDocument()
})

test('the enrolment screen is not a dead end for a session owing a password change', async () => {
  // Every endpoint on that screen is refused with `password_change_required` while one is
  // owed, so rendering it would be a QR code that can never be confirmed.
  stubApi({ signedIn: true, policyOn: true, mustChangePassword: true })

  renderApp('/mfa/enrol')

  expect(await screen.findByText('Set a new password')).toBeInTheDocument()
  expect(screen.queryByText('Set up your second factor')).not.toBeInTheDocument()
})


test('replacing a live factor asks for a current code before showing a new one', async () => {
  // Everything else behind the second factor is protected by it; the factor itself was the
  // one thing that was not, which made a hijacked live session a way to move the account
  // onto somebody else's phone.
  const { calls } = stubApi({ signedIn: true, mfaEnrolled: true, verifiedAgoMs: 60_000 })
  const user = userEvent.setup()

  renderApp('/mfa/enrol')

  expect(await screen.findByText("Confirm it's you")).toBeInTheDocument()
  expect(screen.queryByRole('img', { name: /Scan this/ })).not.toBeInTheDocument()
  expect(calls.some((c) => c.url === '/api/auth/mfa/enrol')).toBe(false)

  await user.type(screen.getByLabelText('Current code'), TOTP_CODE)
  await user.click(screen.getByRole('button', { name: 'Continue' }))

  expect(await screen.findByRole('img', { name: /Scan this/ })).toBeInTheDocument()
  expect(calls.find((c) => c.url === '/api/auth/mfa/enrol')?.body).toEqual({ code: TOTP_CODE })
})

test('a first enrolment goes straight to the QR code', async () => {
  const { calls } = stubApi({ signedIn: true, policyOn: true })

  renderApp('/mfa/enrol')

  expect(await screen.findByRole('img', { name: /Scan this/ })).toBeInTheDocument()
  expect(screen.queryByText("Confirm it's you")).not.toBeInTheDocument()
  expect(calls.find((c) => c.url === '/api/auth/mfa/enrol')?.body).toEqual({})
})
