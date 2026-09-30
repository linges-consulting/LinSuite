import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import App from '@/App'
import { NO_MFA } from './harness'
import { ThemeProvider } from '@/lib/theme'

/** A claimed instance with someone signed in; `health` is what /api/health answers. */
function stubApi(health: Response) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url === '/api/setup/status') return Response.json({ required: false })
      if (url === '/api/auth/me')
        // Capabilities included, because the nav is drawn from them: a fake that names a
        // role but sends no capabilities describes an account the server cannot produce,
        // and this test would then be asserting the sidebar of a user who does not exist.
        // `mode: 'admin'` for the same reason (#114): Settings' nav entry is now hidden
        // outside Admin Mode, and the server never omits this field.
        return Response.json({
          id: 'u1',
          email: 'owner@cedar.example',
          role: 'Administrator',
          capabilities: ['admin', 'roles.manage', 'users.manage', 'catalog.manage'],
          mode: 'admin',
          can_switch_modes: true,
          admin_grant_expires_at: new Date(Date.now() + 900_000).toISOString(),
          admin_hard_limit_at: new Date(Date.now() + 900_000).toISOString(),
          must_change_password: false,
          mfa: NO_MFA,
        })
      // The shell test lands on /clients, which reads the (empty) list.
      if (url.startsWith('/api/customers')) return Response.json({ customers: [], total: 0 })
      return health.clone()
    }),
  )
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

test('home shows backend health from /api/health', async () => {
  stubApi(Response.json({ status: 'ok', database: 'ok' }))

  renderApp()

  expect(await screen.findByText('Connected')).toBeInTheDocument()
  expect(screen.getByText('Online')).toBeInTheDocument()
  expect(fetch).toHaveBeenCalledWith('/api/health')
})

test('home shows a degraded database as unreachable', async () => {
  stubApi(Response.json({ status: 'degraded', database: 'unreachable' }, { status: 503 }))

  renderApp()

  expect(await screen.findByText('Unreachable')).toBeInTheDocument()
  expect(screen.getByText('Online')).toBeInTheDocument()
})

test('shell exposes the primary destinations this account holds', async () => {
  stubApi(Response.json({ status: 'ok', database: 'ok' }))

  renderApp('/clients')

  // Nothing renders until the setup-status check resolves, hence the await. This account
  // holds none of Sell/Billing/Reports' capabilities (`billing.view`, `commission.view`,
  // `billing.manage`) — Catalog is gone outright (#97), replaced by Sell for whoever holds
  // `billing.view`.
  const nav = await screen.findByRole('navigation', { name: 'Primary' })
  for (const label of ['Schedule', 'Clients', 'Settings']) {
    expect(nav).toHaveTextContent(label)
  }
  for (const label of ['Sell', 'Billing', 'Reports']) {
    expect(nav).not.toHaveTextContent(label)
  }
  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Clients')
})
