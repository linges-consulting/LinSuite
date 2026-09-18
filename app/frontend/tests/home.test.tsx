import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import App from '@/App'
import { ThemeProvider } from '@/lib/theme'

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
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ status: 'ok', database: 'ok' })))

  renderApp()

  expect(await screen.findByText('Connected')).toBeInTheDocument()
  expect(screen.getByText('Online')).toBeInTheDocument()
  expect(fetch).toHaveBeenCalledWith('/api/health')
})

test('home shows a degraded database as unreachable', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => Response.json({ status: 'degraded', database: 'unreachable' }, { status: 503 })),
  )

  renderApp()

  expect(await screen.findByText('Unreachable')).toBeInTheDocument()
  expect(screen.getByText('Online')).toBeInTheDocument()
})

test('shell exposes the four primary destinations', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ status: 'ok', database: 'ok' })))

  renderApp('/clients')

  // Nothing renders until the setup-status check resolves, hence the await.
  const nav = await screen.findByRole('navigation', { name: 'Primary' })
  for (const label of ['Schedule', 'Clients', 'Catalog', 'Settings']) {
    expect(nav).toHaveTextContent(label)
  }
  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Clients')
})
