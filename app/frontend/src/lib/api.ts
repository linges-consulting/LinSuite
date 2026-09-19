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
  /**
   * The parsed response body, kept whole. `code` is read off it for the 403 rules, and
   * anything else a screen needs is already here rather than needing a second read of a
   * stream that has been consumed.
   */
  body: unknown
  /**
   * Which kind of 403 this is — `admin_mode_required`, `capability_required` or
   * `password_change_required`. Null for every other status. The server sends it precisely
   * so the client never has to match on the prose in `detail`, which is written for a
   * person and gets reworded.
   */
  code: string | null
  /**
   * When the server will accept another attempt, as a local clock instant — absolute rather
   * than a duration, so a screen that renders it a second later still counts down honestly.
   * Null unless the answer was a 429 carrying `Retry-After`.
   */
  retryAt: number | null
  /** A 429 because the account is locked, rather than because the last attempt was too soon. */
  locked: boolean

  constructor(message: string, res: Response, body: unknown = null) {
    super(message)
    this.status = res.status
    this.body = body
    this.code =
      typeof (body as { code?: unknown })?.code === 'string'
        ? (body as { code: string }).code
        : null
    const retryAfter = Number(res.headers.get('Retry-After'))
    this.retryAt = retryAfter > 0 ? Date.now() + retryAfter * 1000 : null
    // A header rather than an inference from the size of `Retry-After`: the delay ceiling and
    // the shortest lock are both configurable, and an inference would break when one changed.
    this.locked = res.headers.get('X-Account-Locked') === '1'
  }
}

/** The refusal to throw, with everything the server said about it attached.
 *
 * The body is read once: a `Response` body is a stream, so parsing it for the message and
 * again for the code would leave the second read empty. */
async function failure(res: Response, fallback: string): Promise<ApiError> {
  const body = await res
    .json()
    .then((parsed) => parsed ?? null)
    .catch(() => null)
  return new ApiError(problem(body, fallback, res.status), res, body)
}

export async function completeSetup(payload: SetupPayload): Promise<void> {
  const res = await post('/api/setup', payload)
  if (!res.ok) throw await failure(res, 'Setup failed')
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
  /** The role's name, for display. */
  role: string
  /**
   * The capability keys this account holds. Used to leave out what it cannot use — never to
   * decide whether something is allowed. The server re-reads the role on every request and
   * refuses regardless of what this list says.
   */
  capabilities: string[]
  mode: Mode
  /** True only for someone holding both capabilities; the switcher hides entirely otherwise. */
  can_switch_modes: boolean
  /** When the sliding admin window runs out, or null when there is no window. ISO-8601. */
  admin_grant_expires_at: string | null
  /** The ceiling that window never slides past. ISO-8601, or null. */
  admin_hard_limit_at: string | null
  /**
   * The session is real, but nothing except the change-password screen is reachable until a
   * new password is set — the flag an administrator sets, or a business's rotation interval
   * having elapsed. Routing, not this flag, is what enforces it (`App.tsx`).
   */
  must_change_password: boolean
  /** The second factor, as this session stands. Routed on the same way as the flag above. */
  mfa: MfaState
}

export type MfaMethod = 'totp' | 'email'

export type MfaState = {
  enrolled: boolean
  method: MfaMethod | null
  /**
   * This session has presented a password and nothing else. The server refuses everything
   * but the verify screen's own calls, `/me` and logout, so routing sends the browser there
   * and nowhere else — the same shape of gate as `must_change_password`.
   */
  pending: boolean
  /** The business requires a second factor on this account and it has none yet. */
  enrolment_required: boolean
  /** When this session last presented a code. Null until it has. ISO-8601. */
  verified_at: string | null
  /** Whether the business allows emailed codes, so the verify screen knows what to offer. */
  email_otp_allowed: boolean
}

/**
 * Ask for a reset link. Always resolves: the server answers 202 for an address it has never
 * seen as readily as for one it knows, and this screen must not undo that by reporting the
 * difference. The only honest success message is "if that address has an account".
 */
export async function requestPasswordReset(email: string): Promise<void> {
  const res = await post('/api/auth/password-reset/request', { email })
  if (!res.ok) throw await failure(res, 'Could not send the link')
}

/** Spend the link. A 400 means it expired or was already used; ask for another. */
export async function confirmPasswordReset(request: {
  token: string
  new_password: string
}): Promise<void> {
  const res = await post('/api/auth/password-reset/confirm', request)
  if (!res.ok) throw await failure(res, 'Could not set the password')
}

/**
 * Change it while signed in. The response carries a replacement session cookie, so this tab
 * survives the revocation that ends every other session the account holds.
 */
export async function changePassword(request: {
  current_password: string
  new_password: string
}): Promise<User> {
  const res = await post('/api/auth/password/change', request)
  if (!res.ok) throw await failure(res, 'Could not change the password')
  return res.json()
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
export async function switchMode(request: {
  mode: Mode
  password?: string
  totp?: string
}): Promise<User> {
  const res = await post('/api/auth/mode', request)
  if (!res.ok) throw await failure(res, 'Mode switch failed')
  return res.json()
}

/** What a receipt, an invoice and a form header are rendered from (PRD §1). */
export type BusinessProfile = {
  name: string
  address_line1: string | null
  address_line2: string | null
  city: string | null
  /** One of the thirteen two-letter codes, or null until an address is entered. */
  province: string | null
  postal_code: string | null
  phone: string | null
  email: string | null
  gst_hst_number: string | null
  pst_qst_number: string | null
  currency_symbol: string
  receipt_footer: string | null
}

export type Business = BusinessProfile & {
  /** Fixed at `CA`, and not this form's to change. */
  country: string
  /** Changed through its own endpoint, never as part of the profile. */
  timezone: string
  setup_completed_at: string | null
}

/** Admin Mode only. A 403 means the window lapsed — see `createQueryClient`. */
export async function fetchAdminBusiness(): Promise<Business> {
  const res = await fetch('/api/admin/business')
  if (!res.ok) throw await failure(res, 'Could not load the business')
  return res.json()
}

export async function updateBusiness(profile: BusinessProfile): Promise<Business> {
  const res = await send('PUT', '/api/admin/business', profile)
  if (!res.ok) throw await failure(res, 'Could not save the business')
  return res.json()
}

/**
 * Change the zone every recurring rule in the app is read against. Separate from the profile
 * on purpose: this re-interprets data that already exists rather than replacing a field.
 */
export async function updateTimezone(timezone: string): Promise<{ timezone: string }> {
  const res = await send('PATCH', '/api/admin/business/timezone', { timezone })
  if (!res.ok) throw await failure(res, 'Could not change the timezone')
  return res.json()
}

export type Province = {
  code: string
  name: string
  /** The zone most of this province keeps — a suggestion a human confirms, never applied. */
  timezone: string
}

export async function fetchProvinces(): Promise<Province[]> {
  const res = await fetch('/api/admin/business/provinces')
  if (!res.ok) throw await failure(res, 'Could not load the provinces')
  return res.json()
}

// --- branding ---------------------------------------------------------------------------

/** The stored hexes, plus everything the server derives from them. */
export type Branding = {
  brand_primary: string
  brand_secondary: string
  /** `primary`, `primary_dark`, each one's foreground, and the same pair for the secondary. */
  colors: Record<string, string>
  /**
   * Text on each brand surface, in the theme it is used in. Computed server-side beside the
   * derivation it depends on, so the warning on screen and the colour that ships agree.
   */
  contrast: Record<string, number>
}

export async function fetchBranding(): Promise<Branding> {
  const res = await fetch('/api/admin/business/branding')
  if (!res.ok) throw await failure(res, 'Could not load the branding')
  return res.json()
}

/** What the screen draws while somebody is still choosing. Nothing is saved. */
export async function previewBranding(colours: {
  brand_primary: string
  brand_secondary: string
}): Promise<Branding> {
  const query = new URLSearchParams(colours)
  const res = await fetch(`/api/admin/business/branding/preview?${query}`)
  if (!res.ok) throw await failure(res, 'Could not preview those colours')
  return res.json()
}

export async function updateBrandColours(colours: {
  brand_primary: string
  brand_secondary: string
}): Promise<Branding> {
  const res = await send('PUT', '/api/admin/business/branding', colours)
  if (!res.ok) throw await failure(res, 'Could not save the colours')
  return res.json()
}

export type BrandingAssetKind = 'logo' | 'favicon'

/**
 * The one request in the application that is not `application/json`. A file cannot be, and
 * the server allowlists exactly these two paths for it — the browser sends `Origin` on a
 * multipart POST, which is what stands in for the JSON-only rule there.
 *
 * `Content-Type` is deliberately unset: the browser has to write the multipart boundary.
 */
export async function uploadBrandingAsset(
  kind: BrandingAssetKind,
  file: File,
): Promise<{ url: string; etag: string; width: number; height: number }> {
  const body = new FormData()
  body.append('file', file)
  const res = await fetch(`/api/admin/business/${kind}`, { method: 'POST', body })
  if (!res.ok) throw await failure(res, `Could not upload the ${kind}`)
  return res.json()
}

export async function removeBrandingAsset(kind: BrandingAssetKind): Promise<void> {
  const res = await send('DELETE', `/api/admin/business/${kind}`)
  if (!res.ok) throw await failure(res, `Could not remove the ${kind}`)
}

/** Everything the shell needs to look like this business. Anonymous: the login screen and
 *  the browser tab are branded before anybody has signed in. */
export type BrandingDocument = {
  name: string
  colors: Record<string, string>
  logo_url: string | null
  logo_etag: string | null
  favicon_url: string | null
  favicon_etag: string | null
}

export async function fetchBrandingDocument(): Promise<BrandingDocument> {
  const res = await fetch('/api/branding')
  if (!res.ok) throw new Error(`Branding failed: HTTP ${res.status}`)
  return res.json()
}

export async function login(credentials: { email: string; password: string }): Promise<User> {
  const res = await post('/api/auth/login', credentials)
  if (!res.ok) throw await failure(res, 'Sign in failed')
  return res.json()
}

export async function logout(): Promise<void> {
  const res = await post('/api/auth/logout', {})
  if (!res.ok) throw await failure(res, 'Sign out failed')
}

/**
 * The session rides in an httpOnly cookie, so the backend refuses any mutating request
 * that is not `application/json` — the one content type a cross-origin HTML form cannot
 * produce. Every write goes through here so that header is never forgotten, DELETE
 * included: the guard covers every mutating method, not just the ones that carry a body.
 */
function send(method: string, url: string, body?: unknown): Promise<Response> {
  return fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

const post = (url: string, body: unknown) => send('POST', url, body)

/** FastAPI's `detail` is a string for our own errors and a list for validation failures. */
function problem(body: unknown, fallback: string, status: number): string {
  const detail = (body as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg
  return `${fallback}: HTTP ${status}`
}

export async function fetchHealth(): Promise<Health> {
  const res = await fetch('/api/health')
  // 503 still carries a valid body; anything else is a real failure.
  if (!res.ok && res.status !== 503) throw new Error(`Health check failed: HTTP ${res.status}`)
  return res.json()
}

// --- roles, capabilities and accounts (Admin Mode) -------------------------------------

/** One entry of the server's capability registry. The list is fetched rather than hard-coded
 *  here, so the toggles on screen are always the ones the server actually enforces. */
export type Capability = {
  key: string
  description: string
  group: string
  requires_admin_mode: boolean
}

export type Role = {
  id: string
  name: string
  description: string
  is_system: boolean
  capabilities: string[]
  /** How many accounts hold it — what disables the delete button, and the reason it gives. */
  user_count: number
}

/**
 * A staff member and the account behind them — one row, because the Staff table shows the
 * role, the lock and the second factor beside the colour and the credentials.
 *
 * `id` is the staff member's, and is what the staff endpoints take. `user_id` is the
 * account's, which the three Task 5 endpoints (`/role`, `/unlock`, `/mfa/reset`) still key on.
 */
export type StaffRow = {
  id: string
  user_id: string
  email: string
  role: string
  role_id: string
  /** When the temporary lock lifts, or null when the account is not locked. ISO-8601. */
  locked_until: string | null
  /** Whether there is a second factor to reset. False makes "Reset MFA" a no-op that would
   *  still sign the person out of every device, so the button is disabled instead. */
  mfa_enrolled: boolean
  first_name: string
  last_name: string
  display_name: string
  is_practitioner: boolean
  /** Required of a practitioner: insurers reject treatment receipts without both. */
  designation: string | null
  licence_number: string | null
  /** Basis points — 4500 is 45%. The form shows percentages and converts. */
  commission_rate_services_bp: number
  commission_rate_retail_bp: number
  /** A key from the server's palette, never a hex. */
  colour: string
  max_concurrent_appointments: number
  active: boolean
  sort_order: number
  /** No password has ever been set: the invitation is still outstanding. */
  invite_pending: boolean
}

/** One curated staff colour. The calendar paints appointments with it, so it is served
 *  rather than chosen in the browser — including the text colour that reads on it. */
export type StaffColour = {
  key: string
  name: string
  hex: string
  dark_hex: string
  foreground: string
  dark_foreground: string
}

/** What the create and edit forms send. `colour` absent on create means "choose one". */
export type StaffDraft = {
  first_name: string
  last_name: string
  display_name: string | null
  is_practitioner: boolean
  designation: string | null
  licence_number: string | null
  commission_rate_services_bp: number
  commission_rate_retail_bp: number
  colour: string | null
  max_concurrent_appointments: number
}

export async function fetchCapabilities(): Promise<Capability[]> {
  const res = await fetch('/api/admin/capabilities')
  if (!res.ok) throw await failure(res, 'Could not load the capabilities')
  return (await res.json()).capabilities
}

export async function fetchRoles(): Promise<Role[]> {
  const res = await fetch('/api/admin/roles')
  if (!res.ok) throw await failure(res, 'Could not load the roles')
  return (await res.json()).roles
}

export type RoleDraft = { name: string; description: string; capabilities: string[] }

export async function createRole(draft: RoleDraft): Promise<Role> {
  const res = await send('POST', '/api/admin/roles', draft)
  if (!res.ok) throw await failure(res, 'Could not create the role')
  return res.json()
}

export async function updateRole(id: string, draft: Partial<RoleDraft>): Promise<Role> {
  const res = await send('PATCH', `/api/admin/roles/${id}`, draft)
  if (!res.ok) throw await failure(res, 'Could not save the role')
  return res.json()
}

export async function deleteRole(id: string): Promise<void> {
  const res = await send('DELETE', `/api/admin/roles/${id}`)
  if (!res.ok) throw await failure(res, 'Could not delete the role')
}

/** Inactive staff are left out unless asked for: they are history, not the roster. */
export async function fetchStaff(includeInactive = false): Promise<StaffRow[]> {
  const res = await fetch(`/api/admin/staff${includeInactive ? '?include_inactive=true' : ''}`)
  if (!res.ok) throw await failure(res, 'Could not load the staff')
  return (await res.json()).staff
}

export async function fetchStaffPalette(): Promise<StaffColour[]> {
  const res = await fetch('/api/admin/staff/palette')
  if (!res.ok) throw await failure(res, 'Could not load the colours')
  return (await res.json()).colours
}

/**
 * Create the staff member and the account together. No password is set and none is sent:
 * the server emails an invitation link, and that is the only way in.
 */
export async function createStaff(
  draft: StaffDraft & { email: string; role_id: string },
): Promise<StaffRow> {
  const res = await send('POST', '/api/admin/staff', draft)
  if (!res.ok) throw await failure(res, 'Could not add the staff member')
  return res.json()
}

export async function updateStaff(id: string, draft: Partial<StaffDraft>): Promise<StaffRow> {
  const res = await send('PATCH', `/api/admin/staff/${id}`, draft)
  if (!res.ok) throw await failure(res, 'Could not save the staff member')
  return res.json()
}

/** Ends every session they hold and refuses the next sign-in. The record stays. */
export async function deactivateStaff(id: string): Promise<StaffRow> {
  const res = await send('POST', `/api/admin/staff/${id}/deactivate`, {})
  if (!res.ok) throw await failure(res, 'Could not deactivate the staff member')
  return res.json()
}

export async function reactivateStaff(id: string): Promise<StaffRow> {
  const res = await send('POST', `/api/admin/staff/${id}/reactivate`, {})
  if (!res.ok) throw await failure(res, 'Could not reactivate the staff member')
  return res.json()
}

/** A fresh invitation link. Every earlier one stops working. */
export async function resendInvite(id: string): Promise<void> {
  const res = await send('POST', `/api/admin/staff/${id}/resend-invite`, {})
  if (!res.ok) throw await failure(res, 'Could not send the invitation')
}

export async function assignRole(userId: string, roleId: string): Promise<{ role: string }> {
  const res = await send('PATCH', `/api/admin/users/${userId}/role`, { role_id: roleId })
  if (!res.ok) throw await failure(res, 'Could not change the role')
  return res.json()
}

/** Reopen a locked account before its lock expires. The offence tier goes with it. */
export async function unlockAccount(userId: string): Promise<void> {
  const res = await send('POST', `/api/admin/users/${userId}/unlock`, {})
  if (!res.ok) throw await failure(res, 'Could not unlock the account')
}


// --- the second factor ------------------------------------------------------------------

export type Enrolment = {
  /** The base32 secret, for somebody whose phone cannot scan. Shown once and never fetched again. */
  secret: string
  /** `otpauth://…`. The QR code is drawn from this in the browser (`components/qr-code.tsx`). */
  provisioning_uri: string
}

export type MfaStatus = {
  enrolled: boolean
  method: MfaMethod | null
  /** A count, never the codes: only their digests survive the one time they were shown. */
  recovery_codes_remaining: number
  email_otp_allowed: boolean
  required_for_admin: boolean
}

/**
 * Begin an enrolment. `code` is a current authenticator or recovery code, and the server
 * requires one when there is already a factor being replaced — swapping the second factor is
 * the one thing behind it that was not itself protected by it.
 */
export async function startEnrolment(code?: string): Promise<Enrolment> {
  const res = await post('/api/auth/mfa/enrol', code ? { code } : {})
  if (!res.ok) throw await failure(res, 'Could not start the enrolment')
  return res.json()
}

/** The code is what makes the enrolment real. The recovery codes come back once, here. */
export async function confirmEnrolment(code: string): Promise<string[]> {
  const res = await post('/api/auth/mfa/enrol/confirm', { code })
  if (!res.ok) throw await failure(res, 'Could not confirm the code')
  return (await res.json()).recovery_codes
}

export async function startEmailEnrolment(code?: string): Promise<void> {
  const res = await post('/api/auth/mfa/enrol/email', code ? { code } : {})
  if (!res.ok) throw await failure(res, 'Could not send the code')
}

export async function confirmEmailEnrolment(code: string): Promise<string[]> {
  const res = await post('/api/auth/mfa/enrol/email/confirm', { code })
  if (!res.ok) throw await failure(res, 'Could not confirm the code')
  return (await res.json()).recovery_codes
}

/**
 * Finish signing in. One field takes an authenticator code, an emailed code or a recovery
 * code — the person typing knows which they are holding, and the server tells them apart.
 *
 * A wrong code is a 403 with `invalid_mfa_code`, and it feeds the same per-account lockout a
 * wrong password does, so a 429 is possible here too.
 */
export async function verifyMfa(code: string): Promise<User> {
  const res = await post('/api/auth/mfa/verify', { code })
  if (!res.ok) throw await failure(res, 'Could not verify the code')
  return res.json()
}

export async function requestEmailOtp(): Promise<void> {
  const res = await post('/api/auth/mfa/email-otp/request', {})
  if (!res.ok) throw await failure(res, 'Could not send the code')
}

export async function fetchMfaStatus(): Promise<MfaStatus> {
  const res = await fetch('/api/auth/mfa')
  if (!res.ok) throw await failure(res, 'Could not load your security settings')
  return res.json()
}

/** A fresh set. Everything issued before stops working the moment this returns. */
export async function regenerateRecoveryCodes(): Promise<string[]> {
  const res = await post('/api/auth/mfa/recovery-codes', {})
  if (!res.ok) throw await failure(res, 'Could not issue new recovery codes')
  return (await res.json()).recovery_codes
}

/** Admin Mode + `users.manage`. Clears the enrolment and signs the account out everywhere. */
export async function resetUserMfa(userId: string): Promise<void> {
  const res = await post(`/api/admin/users/${userId}/mfa/reset`, {})
  if (!res.ok) throw await failure(res, 'Could not reset the second factor')
}

export type SecurityPolicy = {
  mfa_required_for_admin: boolean
  mfa_email_otp_allowed: boolean
}

export async function fetchSecurityPolicy(): Promise<SecurityPolicy> {
  const res = await fetch('/api/admin/business/security')
  if (!res.ok) throw await failure(res, 'Could not load the security policy')
  return res.json()
}

export async function updateSecurityPolicy(policy: SecurityPolicy): Promise<SecurityPolicy> {
  const res = await send('PATCH', '/api/admin/business/security', policy)
  if (!res.ok) throw await failure(res, 'Could not save the security policy')
  return res.json()
}
