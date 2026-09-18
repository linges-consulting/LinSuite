import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import App from '@/App'
import { ThemeProvider } from '@/lib/theme'

export type Call = { url: string; method: string; body?: unknown; contentType?: string }

type Api = { signedIn?: boolean; setupRequired?: boolean; loginResponse?: () => Response }

/**
 * A claimed instance's API, in the three states routing cares about.
 *
 * The session lives in an httpOnly cookie no test can see, so `signedIn` stands in for it:
 * it is what `/api/auth/me` answers, and login and logout flip it the way the real cookie
 * would. The returned array is every request the app made, in order.
 */
export function stubApi({ signedIn = false, setupRequired = false, loginResponse }: Api = {}) {
  const calls: Call[] = []
  const account = { id: 'u1', email: 'owner@cedar.example', is_admin: true }
  let session = signedIn

  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const headers = (init?.headers ?? {}) as Record<string, string>
      calls.push({
        url,
        method: init?.method ?? 'GET',
        body: init?.body ? JSON.parse(init.body as string) : undefined,
        contentType: headers['Content-Type'],
      })
      if (url === '/api/setup/status') return Response.json({ required: setupRequired })
      if (url === '/api/auth/me') {
        if (!session) return Response.json({ detail: 'Not authenticated' }, { status: 401 })
        return Response.json(account)
      }
      if (url === '/api/auth/login') {
        const rejection = loginResponse?.()
        if (rejection) return rejection
        session = true
        return Response.json(account)
      }
      if (url === '/api/auth/logout') {
        session = false
        return new Response(null, { status: 204 })
      }
      return Response.json({ status: 'ok', database: 'ok' })
    }),
  )
  return calls
}

export function renderApp(path = '/') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>
    </ThemeProvider>,
  )
}
