import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp, stubApi } from './harness'

async function signIn(
  user: ReturnType<typeof userEvent.setup>,
  password = 'correct horse battery',
) {
  await user.type(await screen.findByLabelText('Email'), 'owner@cedar.example')
  await user.type(screen.getByLabelText('Password'), password)
  await user.click(screen.getByRole('button', { name: 'Sign in' }))
}

afterEach(() => vi.unstubAllGlobals())

// --- redirects ---------------------------------------------------------------------------

test('an anonymous visitor to a protected route lands on the login screen', async () => {
  stubApi()

  renderApp('/clients')

  expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  expect(screen.queryByRole('navigation', { name: 'Primary' })).not.toBeInTheDocument()
})

test('a signed-in visitor gets the shell, not the login screen', async () => {
  stubApi({ signedIn: true })

  renderApp('/login')

  expect(await screen.findByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
  expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
})

test('an unclaimed instance sends even /login to the wizard', async () => {
  stubApi({ setupRequired: true })

  renderApp('/login')

  expect(await screen.findByText('Unlock setup')).toBeInTheDocument()
})

// --- signing in --------------------------------------------------------------------------

test('correct credentials post one JSON body and open the shell', async () => {
  const calls = stubApi()
  const user = userEvent.setup()

  renderApp('/login')
  await signIn(user)

  expect(await screen.findByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
  expect(calls.filter((c) => c.url === '/api/auth/login')).toEqual([
    {
      url: '/api/auth/login',
      method: 'POST',
      // The backend refuses a form-encoded body; this header is the other half of that.
      contentType: 'application/json',
      body: { email: 'owner@cedar.example', password: 'correct horse battery' },
    },
  ])
})

test('rejected credentials show the server’s own answer and keep the visitor out', async () => {
  // Never a friendlier message: "no such account" would hand back the account-enumeration
  // answer the backend deliberately withheld.
  stubApi({
    loginResponse: () => Response.json({ detail: 'Incorrect email or password' }, { status: 401 }),
  })
  const user = userEvent.setup()

  renderApp('/login')
  await signIn(user, 'not the password')

  expect(await screen.findByText('Incorrect email or password')).toBeInTheDocument()
  expect(screen.queryByRole('navigation', { name: 'Primary' })).not.toBeInTheDocument()
})
