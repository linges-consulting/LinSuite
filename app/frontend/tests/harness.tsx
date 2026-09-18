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
/** The one reset token this fake accepts; anything else is expired or spent. */
export const LIVE_RESET_TOKEN = 'a-live-reset-token'

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
  /** The forced-change flag, as `/me` and `/login` report it. */
  mustChangePassword?: boolean
  /**
   * An answer the test chooses, consulted before the fake's own behaviour. It exists for the
   * refusals the fake has no state for — being throttled is the server's business, and the
   * browser only ever sees the 429 it sends back.
   */
  respond?: (url: string, body: any) => Response | undefined
}

/** A throttled or locked-out refusal, shaped exactly as `auth/throttle.py` sends it. */
export function tooManyRequests(seconds: number, { locked = false } = {}): Response {
  const headers: Record<string, string> = { 'Retry-After': String(seconds) }
  if (locked) headers['X-Account-Locked'] = '1'
  return Response.json(
    {
      detail: locked
        ? 'This account is temporarily locked after too many failed attempts.'
        : `Too many attempts. Try again in ${seconds} seconds.`,
    },
    { status: 429, headers },
  )
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
  mustChangePassword = false,
  respond,
}: Api = {}) {
  const calls: Call[] = []
  let session = signedIn
  let mustChange = mustChangePassword
  let password = PASSWORD
  let mode = adminWindowMs > 0 ? 'admin' : 'staff'
  let grantExpiresAt = adminWindowMs > 0 ? Date.now() + adminWindowMs : 0

  const granted = () => grantExpiresAt > Date.now()
  const account = () => ({
    id: 'u1',
    email: 'owner@cedar.example',
    role: dualRole ? 'Administrator' : 'Staff',
    capabilities: dualRole
      ? ['admin', 'roles.manage', 'users.manage', 'schedule.view']
      : ['schedule.view', 'customers.view'],
    mode: granted() ? mode : 'staff',
    can_switch_modes: dualRole,
    admin_grant_expires_at: granted() ? new Date(grantExpiresAt).toISOString() : null,
    admin_hard_limit_at: granted() ? new Date(grantExpiresAt + ADMIN_WINDOW_MS).toISOString() : null,
    must_change_password: mustChange,
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
      const chosen = respond?.(url, body)
      if (chosen) return chosen
      if (url === '/api/setup/status') return Response.json({ required: setupRequired })
      // Both reset endpoints are anonymous, so they sit above the session check below.
      if (url === '/api/auth/password-reset/request') {
        // 202 whatever the address — the fake withholds the same answer the server does.
        return Response.json({ status: 'accepted' }, { status: 202 })
      }
      if (url === '/api/auth/password-reset/confirm') {
        if (body.token !== LIVE_RESET_TOKEN) {
          return Response.json(
            { detail: 'This reset link has expired or has already been used.' },
            { status: 400 },
          )
        }
        return new Response(null, { status: 204 })
      }
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
      if (url === '/api/auth/password/change') {
        if (body.current_password !== password) {
          // Not a capability or mode refusal — a wrong current password. No `code`, so the
          // query client leaves it to the screen that asked.
          return Response.json({ detail: 'Incorrect password' }, { status: 403 })
        }
        if (body.new_password.length < 12) {
          return Response.json(
            { detail: [{ type: 'value_error', loc: ['body', 'new_password'], msg: 'Use at least 12 characters.' }] },
            { status: 422 },
          )
        }
        // The real endpoint replaces the cookie in this response, so the tab stays signed in.
        password = body.new_password
        mustChange = false
        return Response.json(account())
      }
      if (url === '/api/auth/mode') {
        if (body.mode === 'admin') {
          if (!dualRole) {
            return Response.json(
              { detail: 'Switch to Admin Mode to do this.', code: 'admin_mode_required' },
              { status: 403 },
            )
          }
          if (!granted()) {
            // A request with no password is the lapsed-grant race, not a failed attempt —
            // the server refuses it before it looks at the hash, and records nothing.
            if (body.password === undefined) {
              return Response.json(
                {
                  detail: 'Enter your password to switch to Admin Mode.',
                  code: 'admin_mode_required',
                },
                { status: 403 },
              )
            }
            if (body.password !== PASSWORD) {
              return Response.json(
                { detail: 'Incorrect password', code: 'admin_mode_required' },
                { status: 403 },
              )
            }
            grantExpiresAt = Date.now() + ADMIN_WINDOW_MS
          }
        }
        mode = body.mode
        return Response.json(account())
      }
      if (url === '/api/admin/business') {
        if (account().mode !== 'admin') {
          return Response.json(
            { detail: 'Switch to Admin Mode to do this.', code: 'admin_mode_required' },
            { status: 403 },
          )
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
