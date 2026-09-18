export type Health = { status: 'ok' | 'degraded'; database: 'ok' | 'unreachable' }

export type SetupStatus = { required: boolean }

export type SetupPayload = {
  token: string
  business_name: string
  timezone: string
  admin_email: string
  admin_password: string
}

/** Answers whether this instance is still unclaimed; available before and after setup. */
export async function fetchSetupStatus(): Promise<SetupStatus> {
  const res = await fetch('/api/setup/status')
  if (!res.ok) throw new Error(`Setup status failed: HTTP ${res.status}`)
  return res.json()
}

export async function fetchTimezones(): Promise<string[]> {
  const res = await fetch('/api/setup/timezones')
  if (!res.ok) throw new Error(`Timezone list failed: HTTP ${res.status}`)
  return (await res.json()).timezones
}

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

export async function completeSetup(payload: SetupPayload): Promise<void> {
  const res = await post('/api/setup', payload)
  if (!res.ok) throw new ApiError(await problem(res, 'Setup failed'), res.status)
}

export type Mode = 'staff' | 'admin'

/**
 * The account *and* the session serving it. Mode is server state — the cookie is opaque to
 * this code — so everything the context switcher draws comes from here, including the two
 * instants behind the admin countdown.
 */
export type User = {
  id: string
  email: string
  is_admin: boolean
  mode: Mode
  /** True only for someone holding both capabilities; the switcher hides entirely otherwise. */
  can_switch_modes: boolean
  /** When the sliding admin window runs out, or null when there is no window. ISO-8601. */
  admin_grant_expires_at: string | null
  /** The ceiling that window never slides past. ISO-8601, or null. */
  admin_hard_limit_at: string | null
}

/**
 * The signed-in user, or null when there is no live session. A 401 is the expected answer
 * for an anonymous visitor, so it is a value here and not a thrown error — otherwise every
 * first page load would look like a failure to TanStack Query.
 */
export async function fetchMe(): Promise<User | null> {
  const res = await fetch('/api/auth/me')
  if (res.status === 401) return null
  if (!res.ok) throw new Error(`Session check failed: HTTP ${res.status}`)
  return res.json()
}

/**
 * Enter or leave Admin Mode. The password is needed only to open a window — switching back
 * and forth inside a live one is free, and returning to Staff Mode never asks.
 *
 * A refusal here is a 403, never a 401: the session is fine, it is the elevation that was
 * declined, and a 401 would send someone who mistyped a password back to the login screen.
 */
export async function switchMode(request: { mode: Mode; password?: string }): Promise<User> {
  const res = await post('/api/auth/mode', request)
  if (!res.ok) throw new ApiError(await problem(res, 'Mode switch failed'), res.status)
  return res.json()
}

export type BusinessProfile = {
  name: string
  timezone: string
  setup_completed_at: string | null
}

/** Admin Mode only. A 403 means the window lapsed — see `createQueryClient`. */
export async function fetchAdminBusiness(): Promise<BusinessProfile> {
  const res = await fetch('/api/admin/business')
  if (!res.ok) throw new ApiError(await problem(res, 'Could not load the business'), res.status)
  return res.json()
}

export async function login(credentials: { email: string; password: string }): Promise<User> {
  const res = await post('/api/auth/login', credentials)
  if (!res.ok) throw new ApiError(await problem(res, 'Sign in failed'), res.status)
  return res.json()
}

export async function logout(): Promise<void> {
  const res = await post('/api/auth/logout', {})
  if (!res.ok) throw new ApiError(await problem(res, 'Sign out failed'), res.status)
}

/**
 * The session rides in an httpOnly cookie, so the backend refuses any mutating request
 * that is not `application/json` — the one content type a cross-origin HTML form cannot
 * produce. Every write goes through here so that header is never forgotten.
 */
function post(url: string, body: unknown): Promise<Response> {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

/** FastAPI's `detail` is a string for our own errors and a list for validation failures. */
async function problem(res: Response, fallback: string): Promise<string> {
  const detail = await res
    .json()
    .then((body) => body?.detail)
    .catch(() => null)
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg
  return `${fallback}: HTTP ${res.status}`
}

export async function fetchHealth(): Promise<Health> {
  const res = await fetch('/api/health')
  // 503 still carries a valid body; anything else is a real failure.
  if (!res.ok && res.status !== 503) throw new Error(`Health check failed: HTTP ${res.status}`)
  return res.json()
}
