import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import App from '@/App'
import { NO_MFA } from './harness'
import { ThemeProvider } from '@/lib/theme'

type Api = { claimed?: boolean; signedIn?: boolean; post?: Response }

/** Routes the three setup calls and the session check; anything else is the health check. */
function stubApi({ claimed = false, signedIn = false, post }: Api = {}) {
  const calls: { url: string; body?: unknown }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, body: init?.body ? JSON.parse(init.body as string) : undefined })
      if (url === '/api/auth/me') {
        if (!signedIn) return Response.json({ detail: 'Not authenticated' }, { status: 401 })
        // The wizard's own account: Administrator, and holding what that role holds.
        return Response.json({
          id: 'u1',
          email: 'owner@cedar.example',
          role: 'Administrator',
          capabilities: ['admin', 'roles.manage', 'users.manage', 'catalog.manage'],
          mfa: NO_MFA,
        })
      }
      // status and timezones answer either way; only POST closes once the instance is claimed.
      if (url === '/api/setup/status') return Response.json({ required: !claimed })
      if (url === '/api/setup/timezones')
        return Response.json({ timezones: ['America/Toronto', 'Europe/Berlin', 'UTC'] })
      if (url === '/api/setup') {
        if (claimed) return new Response(null, { status: 404 })
        return post ?? Response.json({ status: 'complete' }, { status: 201 })
      }
      return Response.json({ status: 'ok', database: 'ok' })
    }),
  )
  return calls
}

function renderApp(path = '/') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <ThemeProvider>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>
    </ThemeProvider>,
  )
}

afterEach(() => vi.unstubAllGlobals())

test('an unclaimed instance sends every route to the wizard', async () => {
  stubApi()

  renderApp('/clients')

  expect(await screen.findByText('Unlock setup')).toBeInTheDocument()
  expect(screen.getByText('Step 1 of 3')).toBeInTheDocument()
  expect(screen.queryByRole('navigation', { name: 'Primary' })).not.toBeInTheDocument()
})

test('token, business and administrator steps post one setup payload', async () => {
  const calls = stubApi()
  const user = userEvent.setup()

  renderApp('/setup')

  // 1. token
  await user.type(await screen.findByLabelText('Setup token'), 'the-token')
  await user.click(screen.getByRole('button', { name: 'Continue' }))

  // 2. business
  expect(await screen.findByText('Step 2 of 3')).toBeInTheDocument()
  await user.type(screen.getByLabelText('Business name'), 'Cedar Lane Clinic')
  await user.click(screen.getByRole('combobox', { name: 'Timezone' }))
  await user.click(await screen.findByRole('option', { name: 'Europe/Berlin' }))
  await user.click(screen.getByRole('button', { name: 'Continue' }))

  // 3. administrator
  expect(await screen.findByText('Step 3 of 3')).toBeInTheDocument()
  await user.type(screen.getByLabelText('Email'), 'owner@cedar.example')
  await user.type(screen.getByLabelText('Password'), 'correct horse battery')
  await user.type(screen.getByLabelText('Confirm password'), 'correct horse battery')
  await user.click(screen.getByRole('button', { name: 'Create and finish' }))

  // 4. done
  expect(await screen.findByText('Cedar Lane Clinic is ready')).toBeInTheDocument()
  expect(calls.filter((c) => c.url === '/api/setup')).toEqual([
    {
      url: '/api/setup',
      body: {
        token: 'the-token',
        business_name: 'Cedar Lane Clinic',
        timezone: 'Europe/Berlin',
        admin_email: 'owner@cedar.example',
        admin_password: 'correct horse battery',
      },
    },
  ])
})

test('a password under twelve characters never reaches the server', async () => {
  const calls = stubApi()
  const user = userEvent.setup()

  renderApp('/setup')

  await user.type(await screen.findByLabelText('Setup token'), 'the-token')
  await user.click(screen.getByRole('button', { name: 'Continue' }))
  await user.type(await screen.findByLabelText('Business name'), 'Cedar Lane Clinic')
  await user.click(screen.getByRole('button', { name: 'Continue' }))
  await user.type(await screen.findByLabelText('Email'), 'owner@cedar.example')
  await user.type(screen.getByLabelText('Password'), 'eleven char')
  await user.type(screen.getByLabelText('Confirm password'), 'eleven char')
  await user.click(screen.getByRole('button', { name: 'Create and finish' }))

  expect(await screen.findByText('Use at least 12 characters.')).toBeInTheDocument()
  expect(calls.some((c) => c.url === '/api/setup')).toBe(false)
})

test('mismatched passwords are caught before submitting', async () => {
  const calls = stubApi()
  const user = userEvent.setup()

  renderApp('/setup')

  await user.type(await screen.findByLabelText('Setup token'), 'the-token')
  await user.click(screen.getByRole('button', { name: 'Continue' }))
  await user.type(await screen.findByLabelText('Business name'), 'Cedar Lane Clinic')
  await user.click(screen.getByRole('button', { name: 'Continue' }))
  await user.type(await screen.findByLabelText('Email'), 'owner@cedar.example')
  await user.type(screen.getByLabelText('Password'), 'correct horse battery')
  await user.type(screen.getByLabelText('Confirm password'), 'correct horse batteryy')
  await user.click(screen.getByRole('button', { name: 'Create and finish' }))

  expect(await screen.findByText('The two passwords do not match.')).toBeInTheDocument()
  expect(calls.some((c) => c.url === '/api/setup')).toBe(false)
})

test('a rejected token returns to the first step with the reason', async () => {
  stubApi({ post: Response.json({ detail: 'Invalid setup token' }, { status: 403 }) })
  const user = userEvent.setup()

  renderApp('/setup')

  await user.type(await screen.findByLabelText('Setup token'), 'wrong')
  await user.click(screen.getByRole('button', { name: 'Continue' }))
  await user.type(await screen.findByLabelText('Business name'), 'Cedar Lane Clinic')
  await user.click(screen.getByRole('button', { name: 'Continue' }))
  await user.type(await screen.findByLabelText('Email'), 'owner@cedar.example')
  await user.type(screen.getByLabelText('Password'), 'correct horse battery')
  await user.type(screen.getByLabelText('Confirm password'), 'correct horse battery')
  await user.click(screen.getByRole('button', { name: 'Create and finish' }))

  expect(await screen.findByText('Step 1 of 3')).toBeInTheDocument()
  expect(screen.getByText('Invalid setup token')).toBeInTheDocument()

  // The typed answers survive, and the message dies with the token that caused it.
  await user.click(screen.getByRole('button', { name: 'Continue' }))
  expect(await screen.findByLabelText('Business name')).toHaveValue('Cedar Lane Clinic')
  expect(screen.queryByText('Invalid setup token')).not.toBeInTheDocument()
})

test('/setup is a not-found state once status says setup is no longer required', async () => {
  const calls = stubApi({ claimed: true })

  renderApp('/setup')

  expect(await screen.findByText('Setup is already complete')).toBeInTheDocument()
  expect(screen.queryByLabelText('Setup token')).not.toBeInTheDocument()
  // Read from the body, not from a 404.
  expect(calls.some((c) => c.url === '/api/setup/status')).toBe(true)
})

test('a claimed instance with a session renders the app shell, not the wizard', async () => {
  stubApi({ claimed: true, signedIn: true })

  renderApp('/')

  expect(await screen.findByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
})
