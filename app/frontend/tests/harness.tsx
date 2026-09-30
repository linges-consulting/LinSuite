import { QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import App from '@/App'
import { Toaster } from '@/components/ui/sonner'
import type { BrandingDocument } from '@/lib/api'
import { createQueryClient } from '@/lib/query-client'
import { ThemeProvider } from '@/lib/theme'

export type Call = { url: string; method: string; body?: unknown; contentType?: string }

export const PASSWORD = 'correct horse battery'
export const ADMIN_WINDOW_MS = 15 * 60_000
/** The one reset token this fake accepts; anything else is expired or spent. */
export const LIVE_RESET_TOKEN = 'a-live-reset-token'
/** The one authenticator code this fake accepts, and the one recovery code. */
export const TOTP_CODE = '123456'
export const RECOVERY_CODE = 'aaaa1-bbbb2'
export const EMAILED_CODE = '654321'
export const FRESH_CODES = ['ffff1-00001', 'ffff1-00002', 'ffff1-00003']
/** Stands in for the twelve hours of `ADMIN_MFA_INTERVAL_HOURS`. */
export const MFA_INTERVAL_MS = 12 * 3600_000

export const BUSINESS_NAME = 'Cedar Lane Clinic'
export const PRIMARY = '#1d4ed8'
export const PRIMARY_DARK = '#659dff'
/** The one colour this fake reports as failing the 4.5:1 guideline. */
export const LOW_CONTRAST = '#7a7a7a'

/** What `GET /api/branding` answers for a business with a logo and a favicon. */
export const BRANDING_DOCUMENT: BrandingDocument = {
  name: BUSINESS_NAME,
  timezone: 'America/Toronto',
  colors: {
    primary: PRIMARY,
    primary_foreground: '#ffffff',
    primary_dark: PRIMARY_DARK,
    primary_dark_foreground: '#0f172a',
    secondary: '#0f766e',
    secondary_foreground: '#ffffff',
    secondary_dark: '#68b5ac',
    secondary_dark_foreground: '#0f172a',
  },
  logo_url: '/api/branding/logo?v=abc123def456',
  logo_etag: '"abc123"',
  favicon_url: '/api/branding/favicon?v=fed654cba321',
  favicon_etag: '"fed654"',
}

const PROVINCES = [
  { code: 'AB', name: 'Alberta', timezone: 'America/Edmonton' },
  { code: 'BC', name: 'British Columbia', timezone: 'America/Vancouver' },
  { code: 'ON', name: 'Ontario', timezone: 'America/Toronto' },
]

const EMPTY_PROFILE = {
  name: BUSINESS_NAME,
  address_line1: null,
  address_line2: null,
  city: null,
  province: null,
  postal_code: null,
  phone: null,
  email: null,
  gst_hst_number: null,
  pst_qst_number: null,
  currency_symbol: '$',
  receipt_footer: null,
  slot_granularity_minutes: 15,
  booking_horizon_days: 90,
  vip_visit_threshold: 10,
}

/**
 * The server derives the dark variant and the contrast numbers, so the fake does too — the
 * screen never computes them, and a fake that returned a shape the server does not send
 * would be testing a client that could not exist.
 */
function branding(colours: { brand_primary: string; brand_secondary: string }) {
  const ratio = (hex: string) => (hex === LOW_CONTRAST ? 3.1 : 8.6)
  return {
    ...colours,
    colors: {
      ...BRANDING_DOCUMENT.colors,
      primary: colours.brand_primary,
      secondary: colours.brand_secondary,
    },
    contrast: {
      primary: ratio(colours.brand_primary),
      primary_dark: ratio(colours.brand_primary),
      secondary: ratio(colours.brand_secondary),
      secondary_dark: ratio(colours.brand_secondary),
    },
  }
}

/**
 * An account with no second factor and nothing owed, for the fakes that are about something
 * else entirely. The server always sends this block, so a fake that leaves it out describes
 * an account the server cannot produce — the same reason these fakes carry `capabilities`.
 */
export const NO_MFA = {
  enrolled: false,
  method: null,
  pending: false,
  enrolment_required: false,
  verified_at: null,
  email_otp_allowed: false,
} as const

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
   * The account's second factor, as the server holds it. `enrolled` makes every login
   * pending until a code is presented; `policyOn` is the business requiring one, which
   * gates an unenrolled administrator into the enrolment screen.
   */
  mfaEnrolled?: boolean
  mfaMethod?: 'totp' | 'email'
  policyOn?: boolean
  emailOtpAllowed?: boolean
  /**
   * Milliseconds since this session last presented a code, or null for "never". Past
   * `MFA_INTERVAL_MS` and entering Admin Mode asks for one — which is the twelve-hourly
   * challenge, compressed so a test does not have to wait for it.
   */
  verifiedAgoMs?: number | null
  /**
   * Opt-in only (#117, spec #113): a *forced* TOTP or email enrolment (`policyOn`, not yet
   * `enrolled`) also opens a live Admin Mode window, the way `try_first_run_admin_grant`
   * does on the real server. Off by default so every other `policyOn` test keeps landing in
   * Staff Mode exactly as it always has — an account that lands in Admin Mode pulls in
   * `ModeSwitcher`'s countdown and `useSession`'s five-second poll, which only the one test
   * about this feature needs running.
   */
  firstRunGrant?: boolean
  /**
   * An answer the test chooses, consulted before the fake's own behaviour. It exists for the
   * refusals the fake has no state for — being throttled is the server's business, and the
   * browser only ever sees the 429 it sends back.
   */
  respond?: (url: string, body: any) => Response | undefined
  /** What `GET /api/branding` answers; defaults to a business with both images set. */
  brandingDocument?: BrandingDocument
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
  /** Pushes this session's verification past the interval, so Admin Mode asks for a code. */
  ageMfaVerification: () => void
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
  mfaEnrolled = false,
  mfaMethod = 'totp',
  policyOn = false,
  emailOtpAllowed = false,
  verifiedAgoMs = null,
  firstRunGrant = false,
  respond,
  brandingDocument,
}: Api = {}) {
  const calls: Call[] = []
  let timezone = 'America/Toronto'
  let business = { ...EMPTY_PROFILE }
  let brandColours = { brand_primary: PRIMARY, brand_secondary: '#0f766e' }
  let session = signedIn
  let mustChange = mustChangePassword
  let password = PASSWORD
  let mode = adminWindowMs > 0 ? 'admin' : 'staff'
  let grantExpiresAt = adminWindowMs > 0 ? Date.now() + adminWindowMs : 0
  // Server state, exactly as it is on the real thing: enrolment on the account, pending and
  // verified-at on the session, and a live emailed code with a life of its own.
  let enrolled = mfaEnrolled
  let method: 'totp' | 'email' | null = mfaEnrolled ? mfaMethod : null
  // A session that starts signed in has not presented a code unless the test says when it
  // did — which is what `verifiedAgoMs` means, and the only way to start past the gate.
  let pending = mfaEnrolled && signedIn && verifiedAgoMs === null
  let verifiedAt = verifiedAgoMs === null ? null : Date.now() - verifiedAgoMs
  let liveRecoveryCodes = [RECOVERY_CODE]
  let emailedCode: string | null = null
  // The server spends the TOTP step (RFC 6238 §5.2), so a code works once. The fake holds
  // the same rule, or a test could pass here against a server that would refuse it.
  let spentCode: string | null = null

  const granted = () => grantExpiresAt > Date.now()
  const account = () => ({
    id: 'u1',
    email: 'owner@cedar.example',
    role: dualRole ? 'Administrator' : 'Staff',
    capabilities: dualRole
      ? ['admin', 'roles.manage', 'users.manage', 'catalog.manage', 'schedule.view']
      : ['schedule.view', 'customers.view'],
    mode: granted() ? mode : 'staff',
    can_switch_modes: dualRole,
    admin_grant_expires_at: granted() ? new Date(grantExpiresAt).toISOString() : null,
    admin_hard_limit_at: granted() ? new Date(grantExpiresAt + ADMIN_WINDOW_MS).toISOString() : null,
    must_change_password: mustChange,
    mfa: {
      enrolled,
      method,
      pending,
      enrolment_required: !enrolled && policyOn && dualRole,
      verified_at: verifiedAt === null ? null : new Date(verifiedAt).toISOString(),
      email_otp_allowed: emailOtpAllowed,
    },
  })

  /** The one coded 403 shape the server emits, so nothing here invents a code. */
  const forbidden = (code: string, detail: string) =>
    Response.json({ detail, code }, { status: 403 })

  const server: FakeServer = {
    expireAdminWindow: () => {
      grantExpiresAt = 0
      mode = 'staff'
    },
    endSession: () => {
      session = false
    },
    ageMfaVerification: () => {
      verifiedAt = Date.now() - MFA_INTERVAL_MS - 1000
    },
  }

  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const headers = (init?.headers ?? {}) as Record<string, string>
      // A file upload is the one request that is not JSON; nothing here reads its body.
      const body =
        typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
      calls.push({ url, method: init?.method ?? 'GET', body, contentType: headers['Content-Type'] })
      const chosen = respond?.(url, body)
      if (chosen) return chosen
      if (url === '/api/setup/status') return Response.json({ required: setupRequired })
      // Anonymous, and asked for on every boot: the tab and the login screen are branded
      // before there is a session. It sits above the session check for that reason.
      if (url === '/api/branding') return Response.json(brandingDocument ?? BRANDING_DOCUMENT)
      if (url === '/api/setup/timezones') {
        return Response.json({ timezones: ['America/Toronto', 'America/Vancouver'] })
      }
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
        pending = enrolled
        return Response.json(account())
      }
      if (url === '/api/auth/logout') {
        session = false
        pending = false
        return new Response(null, { status: 204 })
      }
      // Everything below needs a session, and says so the one way the app acts on: 401.
      if (!session && (url.startsWith('/api/auth/') || url.startsWith('/api/admin/'))) {
        return Response.json({ detail: 'Not authenticated' }, { status: 401 })
      }
      if (url === '/api/auth/me') return Response.json(account())
      // The two MFA gates, in the server's own order and with its own codes. They sit above
      // every other authenticated endpoint here for the same reason they sit inside
      // `CurrentUser` there: so nothing below has to remember them.
      const MFA_OPEN_WHILE_PENDING = ['/api/auth/mfa/verify', '/api/auth/mfa/email-otp/request']
      if (pending && !MFA_OPEN_WHILE_PENDING.includes(url)) {
        return forbidden(
          'mfa_verification_required',
          'Enter the code from your authenticator to finish signing in.',
        )
      }
      if (account().mfa.enrolment_required && !url.startsWith('/api/auth/mfa/enrol')) {
        return forbidden(
          'mfa_enrolment_required',
          'This business requires a second factor on accounts that can administer it.',
        )
      }
      if (url === '/api/auth/mfa/verify') {
        if (body.code === TOTP_CODE && body.code === spentCode) {
          return forbidden('invalid_mfa_code', 'That code has already been used.')
        }
        if (body.code === TOTP_CODE || body.code === emailedCode) {
          if (body.code === emailedCode) emailedCode = null
          else spentCode = body.code
          pending = false
          verifiedAt = Date.now()
          return Response.json(account())
        }
        if (liveRecoveryCodes.includes(body.code)) {
          liveRecoveryCodes = liveRecoveryCodes.filter((c) => c !== body.code)
          pending = false
          verifiedAt = Date.now()
          return Response.json(account())
        }
        return forbidden('invalid_mfa_code', 'That code is not right, or has already been used.')
      }
      if (url === '/api/auth/mfa/email-otp/request') {
        if (!emailOtpAllowed && method !== 'email' && liveRecoveryCodes.length > 0) {
          return forbidden('mfa_email_otp_not_allowed', 'Emailed codes are not available.')
        }
        emailedCode = EMAILED_CODE
        return Response.json({ status: 'sent' }, { status: 202 })
      }
      if (url === '/api/auth/mfa/enrol') {
        // Replacing a live factor costs a current code; a first enrolment asks for nothing.
        if (enrolled && body?.code !== TOTP_CODE && !liveRecoveryCodes.includes(body?.code)) {
          return body?.code === undefined
            ? forbidden('mfa_required', 'Enter the code from your authenticator.')
            : forbidden('invalid_mfa_code', 'That code is not right.')
        }
        return Response.json({
          secret: 'JBSWY3DPEHPK3PXP',
          provisioning_uri:
            'otpauth://totp/Cedar%20Lane%20Clinic:owner@cedar.example?secret=JBSWY3DPEHPK3PXP&issuer=Cedar%20Lane%20Clinic',
        })
      }
      if (url === '/api/auth/mfa/enrol/confirm') {
        if (body.code !== TOTP_CODE) {
          return forbidden('invalid_mfa_code', 'That code is not right.')
        }
        const wasForced = !enrolled && policyOn && dualRole
        enrolled = true
        method = 'totp'
        verifiedAt = Date.now()
        liveRecoveryCodes = [...FRESH_CODES]
        // #117, spec #113: the one place the real server (`try_first_run_admin_grant`)
        // opens an Admin Mode window with no password — a forced first enrolment, on a
        // business this fake always treats as not having dismissed onboarding yet.
        if (wasForced && firstRunGrant) {
          mode = 'admin'
          grantExpiresAt = Date.now() + ADMIN_WINDOW_MS
        }
        return Response.json({ recovery_codes: FRESH_CODES })
      }
      if (url === '/api/auth/mfa/enrol/email') {
        if (enrolled && body?.code === undefined) {
          return forbidden('mfa_required', 'Enter the code from your authenticator.')
        }
        if (!emailOtpAllowed) {
          return forbidden('mfa_email_otp_not_allowed', 'Emailed codes are not available.')
        }
        emailedCode = EMAILED_CODE
        return Response.json({ status: 'sent' }, { status: 202 })
      }
      if (url === '/api/auth/mfa/enrol/email/confirm') {
        if (body.code !== emailedCode) return forbidden('invalid_mfa_code', 'That code is not right.')
        const wasForced = !enrolled && policyOn && dualRole
        emailedCode = null
        enrolled = true
        method = 'email'
        verifiedAt = Date.now()
        liveRecoveryCodes = [...FRESH_CODES]
        if (wasForced && firstRunGrant) {
          mode = 'admin'
          grantExpiresAt = Date.now() + ADMIN_WINDOW_MS
        }
        return Response.json({ recovery_codes: FRESH_CODES })
      }
      if (url === '/api/auth/mfa' && (init?.method ?? 'GET') === 'GET') {
        return Response.json({
          enrolled,
          method,
          recovery_codes_remaining: liveRecoveryCodes.length,
          email_otp_allowed: emailOtpAllowed,
          required_for_admin: policyOn,
        })
      }
      if (url === '/api/auth/mfa/recovery-codes') {
        liveRecoveryCodes = [...FRESH_CODES]
        return Response.json({ recovery_codes: FRESH_CODES })
      }
      if (url === '/api/auth/password/change') {
        if (body.current_password !== password) {
          // A wrong current password. It carries a code like every 403 the server sends,
          // and it is deliberately one the query client does not act on — the form that
          // collected the password is what should react.
          return Response.json(
            { detail: 'Incorrect password', code: 'invalid_password' },
            { status: 403 },
          )
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
          // The twelve-hourly challenge, checked before the password like the server does,
          // so a right password never opens a window a missing code should have refused.
          const challengeDue =
            enrolled && (verifiedAt === null || verifiedAt < Date.now() - MFA_INTERVAL_MS)
          if (challengeDue) {
            if (body.totp === undefined) {
              return forbidden(
                'mfa_required',
                'Enter the code from your authenticator to switch to Admin Mode.',
              )
            }
            if (body.totp !== TOTP_CODE && !liveRecoveryCodes.includes(body.totp)) {
              return forbidden('invalid_mfa_code', 'That code is not right.')
            }
            verifiedAt = Date.now()
          }
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
              // `invalid_password`, never `admin_mode_required`: the window is not the
              // problem, the typing is. Labelling it the other way would have the query
              // client toast "Admin Mode expired" at somebody who simply mistyped.
              return Response.json(
                { detail: 'Incorrect password', code: 'invalid_password' },
                { status: 403 },
              )
            }
            grantExpiresAt = Date.now() + ADMIN_WINDOW_MS
          }
        }
        mode = body.mode
        return Response.json(account())
      }
      if (url.startsWith('/api/admin/business')) {
        if (account().mode !== 'admin') {
          return Response.json(
            { detail: 'Switch to Admin Mode to do this.', code: 'admin_mode_required' },
            { status: 403 },
          )
        }
        if (url === '/api/admin/business/provinces') return Response.json(PROVINCES)
        if (url === '/api/admin/business/timezone') {
          timezone = body.timezone
          return Response.json({ timezone })
        }
        if (url === '/api/admin/business') {
          if (init?.method === 'PUT') business = { ...business, ...body }
          return Response.json({
            ...business,
            country: 'CA',
            timezone,
            setup_completed_at: '2026-01-05T12:00:00Z',
          })
        }
        if (url.startsWith('/api/admin/business/branding')) {
          const wanted = url.includes('?')
            ? Object.fromEntries(new URLSearchParams(url.split('?')[1]))
            : (body ?? brandColours)
          if (init?.method === 'PUT') brandColours = body
          return Response.json(branding(wanted as typeof brandColours))
        }
      }
      // An instance with nobody on its books yet; a test with clients answers via `respond`.
      if (url.startsWith('/api/customers?')) return Response.json({ customers: [], total: 0 })
      // #116: every step already done and dismissed, so Home's checklist and email banner stay
      // out of every test that isn't specifically about onboarding — those answer this URL
      // themselves via `respond`, which is checked before this fallback runs.
      if (url === '/api/admin/onboarding') {
        return Response.json({
          steps: (
            ['business', 'hours', 'tax', 'spaces', 'services', 'staff', 'email', 'branding'] as const
          ).map((key) => ({ key, done: true, optional: key === 'branding' })),
          dismissed_at: '2026-01-01T00:00:00Z',
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
