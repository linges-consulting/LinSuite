import { QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import App from '@/App'
import { Toaster } from '@/components/ui/sonner'
import { createQueryClient } from '@/lib/query-client'
import { ThemeProvider } from '@/lib/theme'

export type Call = { url: string; method: string; body?: unknown; contentType?: string }

export const PASSWORD = 'correct horse battery'
export const ADMIN_WINDOW_MS = 15 * 60_000

type Api = {
  signedIn?: boolean
  setupRequired?: boolean
  loginResponse?: () => Response
  /** Whether the account holds both capabilities. False makes it staff-only. */
  dualRole?: boolean
  /**
   * Milliseconds of admin window left when the test starts; 0 means no live grant. A
   * session that has one starts in Admin Mode, the way it would after a switch.
   */
  adminWindowMs?: number
}

export type FakeServer = {
  /** Ends the admin window the way an idle timeout does: server-side, with no warning. */
  expireAdminWindow: () => void
  /** Ends the whole session the way an expired or revoked cookie does. */
  endSession: () => void
}

/**
 * A claimed instance's API, including the parts of the mode state machine the UI can see.
 *
 * The session lives in an httpOnly cookie no test can see, and so does the admin grant —
 * both are server state, so this fake holds them the same way the server does: the grant is
 * an instant, the mode is only honoured while that instant is in the future, and switching
 * to Admin Mode costs a password exactly when there is no live grant.
 */
export function stubApi({
  signedIn = false,
  setupRequired = false,
  loginResponse,
  dualRole = true,
  adminWindowMs = 0,
}: Api = {}) {
  const calls: Call[] = []
  let session = signedIn
  let mode = adminWindowMs > 0 ? 'admin' : 'staff'
  let grantExpiresAt = adminWindowMs > 0 ? Date.now() + adminWindowMs : 0

  const granted = () => grantExpiresAt > Date.now()
  const account = () => ({
    id: 'u1',
    email: 'owner@cedar.example',
    is_admin: dualRole,
    mode: granted() ? mode : 'staff',
    can_switch_modes: dualRole,
    admin_grant_expires_at: granted() ? new Date(grantExpiresAt).toISOString() : null,
    admin_hard_limit_at: granted() ? new Date(grantExpiresAt + ADMIN_WINDOW_MS).toISOString() : null,
  })

  const server: FakeServer = {
    expireAdminWindow: () => {
      grantExpiresAt = 0
      mode = 'staff'
    },
    endSession: () => {
      session = false
    },
  }

  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const headers = (init?.headers ?? {}) as Record<string, string>
      const body = init?.body ? JSON.parse(init.body as string) : undefined
      calls.push({ url, method: init?.method ?? 'GET', body, contentType: headers['Content-Type'] })
      if (url === '/api/setup/status') return Response.json({ required: setupRequired })
      if (url === '/api/auth/login') {
        const rejection = loginResponse?.()
        if (rejection) return rejection
        session = true
        return Response.json(account())
      }
      if (url === '/api/auth/logout') {
        session = false
        return new Response(null, { status: 204 })
      }
      // Everything below needs a session, and says so the one way the app acts on: 401.
      if (!session && (url.startsWith('/api/auth/') || url.startsWith('/api/admin/'))) {
        return Response.json({ detail: 'Not authenticated' }, { status: 401 })
      }
      if (url === '/api/auth/me') return Response.json(account())
      if (url === '/api/auth/mode') {
        if (body.mode === 'admin') {
          if (!dualRole) {
            return Response.json({ detail: 'Switch to Admin Mode to do this.' }, { status: 403 })
          }
          if (!granted()) {
            // A request with no password is the lapsed-grant race, not a failed attempt —
            // the server refuses it before it looks at the hash, and records nothing.
            if (body.password === undefined) {
              return Response.json(
                { detail: 'Enter your password to switch to Admin Mode.' },
                { status: 403 },
              )
            }
            if (body.password !== PASSWORD) {
              return Response.json({ detail: 'Incorrect password' }, { status: 403 })
            }
            grantExpiresAt = Date.now() + ADMIN_WINDOW_MS
          }
        }
        mode = body.mode
        return Response.json(account())
      }
      if (url === '/api/admin/business') {
        if (account().mode !== 'admin') {
          return Response.json({ detail: 'Switch to Admin Mode to do this.' }, { status: 403 })
        }
        return Response.json({
          name: 'Cedar Lane Clinic',
          timezone: 'America/Toronto',
          setup_completed_at: '2026-01-05T12:00:00Z',
        })
      }
      return Response.json({ status: 'ok', database: 'ok' })
    }),
  )
  return { calls, server }
}

export function renderApp(path = '/') {
  // The real client, so the 401 and 403 rules it carries are under test too.
  return render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>
        <Toaster position="bottom-right" />
      </QueryClientProvider>
    </ThemeProvider>,
  )
}
