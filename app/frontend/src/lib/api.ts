import type { Answers, FieldType, FormSchema } from '@/lib/forms'

export type DiagramId = 'body_front' | 'body_back' | 'layout'
export type NoteTemplateDraft = {
  name: string
  fields: { key: string; label: string; required: boolean }[]
  diagram_ids: DiagramId[]
  active: boolean
}
export type NoteTemplate = NoteTemplateDraft & { id: string }
export type NoteAnnotation = {
  id: string; kind: 'pin' | 'zone' | 'text'; diagram_id: DiagramId
  x: number; y: number; colour: string; timestamp: string; text: string
  width?: number | null; height?: number | null
}
export type NoteContent = { answers: Record<string, string>; annotations: NoteAnnotation[] }
export type SessionNote = {
  id: string; template_id: string; appointment_id: string; author_staff_id: string; author_name: string
  template: NoteTemplateDraft; revision: number; created_at: string; updated_at: string
  locked_at: string | null; can_edit: boolean
}
export type NoteAppointment = { id: string; starts_at: string; service_name: string }

async function noteRequest<T>(url: string, method = 'GET', body?: unknown): Promise<T> {
  const res = method === 'GET' ? await fetch(url, { cache: 'no-store' }) : await send(method, url, body)
  if (!res.ok) throw await failure(res, 'Could not update the session record')
  return res.json()
}
export const fetchNoteTemplates = (admin = false) => noteRequest<NoteTemplate[]>(admin ? '/api/admin/note-templates' : '/api/note-templates')
export const saveNoteTemplate = (body: NoteTemplateDraft, id?: string) => noteRequest<NoteTemplate>(`/api/note-templates${id ? `/${id}` : ''}`, id ? 'PUT' : 'POST', body)
const notePath = (customerId: string, id?: string) => `/api/customers/${customerId}/session-notes${id ? `/${id}` : ''}`
export const fetchSessionNotes = (customerId: string) => noteRequest<SessionNote[]>(notePath(customerId))
export const fetchSessionNote = (customerId: string, id: string) => noteRequest<SessionNote & NoteContent>(notePath(customerId, id))
export const fetchNoteAppointments = (customerId: string) => noteRequest<NoteAppointment[]>(`/api/customers/${customerId}/session-note-appointments`)
export const createSessionNote = (customerId: string, body: NoteContent & { appointment_id: string; template_id: string; template: NoteTemplateDraft }) => noteRequest<SessionNote>(notePath(customerId), 'POST', body)
export const updateSessionNote = (customerId: string, id: string, body: NoteContent & { revision: number }) => noteRequest<SessionNote>(notePath(customerId, id), 'PUT', body)
export const lockSessionNote = (customerId: string, id: string, revision: number) => noteRequest<SessionNote>(`${notePath(customerId, id)}/lock`, 'POST', { revision })

/** FastAPI's 422 says which field it is unhappy about in `loc`; put the message under it.
 *  Shared by every edit form that shows a server error inline rather than as a toast.
 *
 *  A custom `field_validator` that raises `ValueError` (every hand-written message in this
 *  app — "date of birth cannot be in the future", and the rest) comes back from Pydantic v2
 *  prefixed `"Value error, "`; that prefix is the exception's type, not part of the sentence
 *  anybody wrote, and reads as a bug report rather than a validation message under a field. */
export function fieldErrors(error: unknown): Record<string, string> {
  const detail = (error as { body?: { detail?: unknown } })?.body?.detail
  if (!Array.isArray(detail)) return {}
  return Object.fromEntries(
    detail
      .filter((entry) => Array.isArray(entry.loc) && entry.loc[0] === 'body')
      .map((entry) => [
        String(entry.loc[1]),
        String(entry.msg).replace(/^Value error, /, ''),
      ]),
  )
}

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
   * A machine-readable refusal code, read straight off the body whenever the server sent
   * one — not only on a 403 (`admin_mode_required`, `capability_required`,
   * `password_change_required`), but on other statuses that need one too (409 `slot_taken`,
   * 422 `not_offered`, 503 `service_unavailable`, and more). Null when the body carries
   * none, so the client never has to match on the prose in `detail`, which is written for a
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
  /** The step bookable starts are offered on, from local midnight: 5–60, in fives. */
  slot_granularity_minutes: number
  /** How far ahead availability is computed at all: 1–365. */
  booking_horizon_days: number
  /** A client becomes "vip" at this many `completed` appointments: 2–1000. */
  vip_visit_threshold: number
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
  // `origin`, not the page's `no-referrer`: under `no-referrer` a browser sends `Origin: null`
  // on a same-origin POST, and the server's upload check needs this deployment's origin.
  const res = await fetch(`/api/admin/business/${kind}`, {
    method: 'POST',
    body,
    referrerPolicy: 'origin',
  })
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
  /** The business's IANA zone — what "today" means on the schedule. */
  timezone: string
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
  /** Only the deactivate response fills this in: confirmed appointments still ahead of the
   *  person. They are not cancelled — the calendar keeps a column for them. */
  future_appointments?: number | null
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

// --- resources: spaces and equipment ----------------------------------------------------

export type ResourceKind = 'space' | 'equipment'

/** A space or a piece of equipment — the physical things a service is delivered in and
 *  with. Both kinds share this shape; only `kind` tells them apart. */
export type ResourceRow = {
  id: string
  kind: ResourceKind
  name: string
  description: string | null
  /** A key from the server's staff palette, or null — a resource need not have one. */
  colour: string | null
  active: boolean
  sort_order: number
}

/** What the create and edit forms send. `kind` is only sent on create — it is fixed once
 *  a resource exists. */
export type ResourceDraft = {
  name: string
  description: string | null
  colour: string | null
  sort_order: number
}

/** Inactive resources are left out unless asked for: they are history, not the picker.
 *  Omitting `kind` returns both, which is what the service requirements builder wants —
 *  one round trip rather than one per tab. */
export async function fetchResources(
  kind?: ResourceKind,
  includeInactive = false,
): Promise<ResourceRow[]> {
  const query = new URLSearchParams(kind ? { kind } : {})
  if (includeInactive) query.set('include_inactive', 'true')
  const res = await fetch(`/api/admin/resources?${query}`)
  if (!res.ok) throw await failure(res, 'Could not load the resources')
  return (await res.json()).resources
}

export async function createResource(
  draft: ResourceDraft & { kind: ResourceKind },
): Promise<ResourceRow> {
  const res = await send('POST', '/api/admin/resources', draft)
  if (!res.ok) throw await failure(res, 'Could not create the resource')
  return res.json()
}

export async function updateResource(
  id: string,
  draft: Partial<ResourceDraft>,
): Promise<ResourceRow> {
  const res = await send('PATCH', `/api/admin/resources/${id}`, draft)
  if (!res.ok) throw await failure(res, 'Could not save the resource')
  return res.json()
}

export async function deactivateResource(id: string): Promise<ResourceRow> {
  const res = await send('POST', `/api/admin/resources/${id}/deactivate`, {})
  if (!res.ok) throw await failure(res, 'Could not deactivate the resource')
  return res.json()
}

export async function reactivateResource(id: string): Promise<ResourceRow> {
  const res = await send('POST', `/api/admin/resources/${id}/reactivate`, {})
  if (!res.ok) throw await failure(res, 'Could not reactivate the resource')
  return res.json()
}

// --- availability: working hours, time off, closures -------------------------------------

/**
 * One working block of the weekly matrix. `weekday` is ISO with Monday = 0, and the two
 * minutes are **minutes since local midnight** — not a UTC instant, and not a time string
 * with an offset. "Works Mondays 09:00–12:00" is a rule about a clock face, and storing it
 * any other way would slide every schedule by an hour at each DST boundary (PRD §1).
 *
 * `end_minute` may be 1440, which is a block that runs to midnight.
 */
export type HoursBlock = {
  weekday: number
  start_minute: number
  end_minute: number
}

export async function fetchStaffHours(staffId: string): Promise<HoursBlock[]> {
  const res = await fetch(`/api/admin/staff/${staffId}/hours`)
  if (!res.ok) throw await failure(res, 'Could not load the working hours')
  return (await res.json()).blocks
}

/** The whole week at once — the screen is a matrix, and a per-block API would let a
 *  half-applied week exist. A 409 means two blocks on one day overlap. */
export async function replaceStaffHours(
  staffId: string,
  blocks: HoursBlock[],
): Promise<HoursBlock[]> {
  const res = await send('PUT', `/api/admin/staff/${staffId}/hours`, { blocks })
  if (!res.ok) throw await failure(res, 'Could not save the working hours')
  return (await res.json()).blocks
}

/**
 * One absence. The instants are what the scheduler reads; the local fields are the same span
 * as somebody entered it, sent by the server so this screen never has to work out which
 * local day an instant falls on.
 */
export type TimeOffEntry = {
  id: string
  all_day: boolean
  reason: string | null
  starts_at: string
  ends_at: string
  starts_at_local: string
  ends_at_local: string
  /** Local calendar dates. For an all-day range, `end_date` is the last day away — inclusive. */
  start_date: string
  end_date: string
}

/** All-day takes local dates; a timed absence takes local datetimes with **no** offset —
 *  the server converts with the business timezone and refuses anything carrying one. */
export type TimeOffDraft =
  | { all_day: true; start_date: string; end_date?: string; reason: string | null }
  | {
      all_day: false
      starts_at_local: string
      ends_at_local: string
      reason: string | null
    }

export async function fetchTimeOff(staffId: string): Promise<TimeOffEntry[]> {
  const res = await fetch(`/api/staff/${staffId}/time-off`)
  if (!res.ok) throw await failure(res, 'Could not load the time off')
  return (await res.json()).time_off
}

export async function createTimeOff(
  staffId: string,
  draft: TimeOffDraft,
): Promise<TimeOffEntry> {
  const res = await send('POST', `/api/staff/${staffId}/time-off`, draft)
  if (!res.ok) throw await failure(res, 'Could not save the time off')
  return res.json()
}

export async function deleteTimeOff(staffId: string, entryId: string): Promise<void> {
  const res = await send('DELETE', `/api/staff/${staffId}/time-off/${entryId}`)
  if (!res.ok) throw await failure(res, 'Could not remove the time off')
}

/** A day the business is shut. `source` is a label, not a rule: a statutory holiday and a
 *  staff retreat block bookings identically. */
export type Closure = {
  id: string
  /** A local calendar date, `YYYY-MM-DD`. */
  date: string
  name: string
  source: 'manual' | 'statutory'
}

export async function fetchClosures(year: number): Promise<Closure[]> {
  const res = await fetch(`/api/admin/closures?year=${year}`)
  if (!res.ok) throw await failure(res, 'Could not load the closures')
  return (await res.json()).closures
}

export async function addClosure(draft: { date: string; name: string }): Promise<Closure> {
  const res = await send('POST', '/api/admin/closures', draft)
  if (!res.ok) throw await failure(res, 'Could not add the closure')
  return res.json()
}

/** A year of the business province's statutory holidays. The only skip rule is "a row for
 *  this date already exists", so pressing it twice is free — and a statutory day somebody
 *  deleted comes back. There are no tombstones; the delete confirmation says so. */
export async function importStatutoryClosures(
  year: number,
): Promise<{ added: number; skipped: number; closures: Closure[] }> {
  const res = await send('POST', `/api/admin/closures/import-statutory?year=${year}`, {})
  if (!res.ok) throw await failure(res, 'Could not import the holidays')
  return res.json()
}

export async function deleteClosure(id: string): Promise<void> {
  const res = await send('DELETE', `/api/admin/closures/${id}`)
  if (!res.ok) throw await failure(res, 'Could not remove the closure')
}

export type RetentionProfile = 'regulated_health' | 'general_business'

export type SecurityPolicy = {
  mfa_required_for_admin: boolean
  mfa_email_otp_allowed: boolean
  retention_profile: RetentionProfile
  /** False until an administrator has saved the profile once — the default is not a choice. */
  retention_profile_chosen: boolean
}

/** A field left out is left alone: the MFA switches and the retention profile save apart. */
export type SecurityChange = Partial<
  Pick<SecurityPolicy, 'mfa_required_for_admin' | 'mfa_email_otp_allowed' | 'retention_profile'>
>

export async function fetchSecurityPolicy(): Promise<SecurityPolicy> {
  const res = await fetch('/api/admin/business/security')
  if (!res.ok) throw await failure(res, 'Could not load the security policy')
  return res.json()
}

export async function updateSecurityPolicy(policy: SecurityChange): Promise<SecurityPolicy> {
  const res = await send('PATCH', '/api/admin/business/security', policy)
  if (!res.ok) throw await failure(res, 'Could not save the security policy')
  return res.json()
}

// --- notifications: sender, SMS, templates, reminders (Phase 12 Task 6, #11) ----------------

export type NotificationTemplate = {
  id: string
  notification_type: string
  channel: 'email' | 'sms'
  subject_template: string | null
  body_template: string
  updated_at: string
  /** The `$identifier`s this type's seeded body actually uses (`notifications/render.py`'s
   *  `safe_substitute` leaves an unrecognised one literal, so this is a courtesy, not a limit). */
  merge_fields: string[]
}

export type NotificationSettings = {
  email_sender: 'resend' | 'smtp' | null
  /** Read straight off `notifications/providers.py::email_ready` — the one fact the banner is
   *  built from, never inferred here from which fields happen to be filled in. */
  email_ready: boolean
  resend_from_address: string | null
  /** Whether a key is stored — the key itself is write-only and never sent to the browser. */
  resend_api_key_set: boolean
  resend_domain_verified_at: string | null
  smtp_host: string | null
  smtp_port: number | null
  smtp_username: string | null
  smtp_from_address: string | null
  smtp_password_set: boolean
  smtp_verified_at: string | null
  sms_enabled: boolean
  sms_ready: boolean
  twilio_account_sid: string | null
  twilio_from_number: string | null
  twilio_auth_token_set: boolean
  reminder_intervals_hours: number[]
  templates: NotificationTemplate[]

  // --- booking-portal policy (Phase 6 Task 4, #10) — read at the API, not just this panel ---
  online_booking_enabled: boolean
  online_cancellation_enabled: boolean
  cancellation_cutoff_hours: number
  booking_daily_cap_per_ip: number
  booking_daily_cap_per_email: number

  // --- walk-in queue toggle (Phase 7 Task 1, #12) — "take a number", not "fit me in" ---
  enable_walk_in_queue: boolean

  // --- CTI demo mode (Phase 14, #16) — off by default, `enable_walk_in_queue`'s exact shape ---
  demo_mode: boolean
}

/** A field left out is left alone — the three secrets included, so rotating one credential
 *  never means retyping the others. `email_sender: 'none'` is the explicit clear; omitting
 *  the field (as every other field can) means "leave it as it is". */
export type NotificationSettingsChange = Partial<{
  email_sender: 'resend' | 'smtp' | 'none'
  resend_from_address: string | null
  resend_api_key: string
  smtp_host: string | null
  smtp_port: number | null
  smtp_username: string | null
  smtp_password: string
  smtp_from_address: string | null
  sms_enabled: boolean
  twilio_account_sid: string | null
  twilio_auth_token: string
  twilio_from_number: string | null
  reminder_intervals_hours: number[]
  online_booking_enabled: boolean
  online_cancellation_enabled: boolean
  cancellation_cutoff_hours: number
  booking_daily_cap_per_ip: number
  booking_daily_cap_per_email: number
  enable_walk_in_queue: boolean
  demo_mode: boolean
}>

export async function fetchNotificationSettings(): Promise<NotificationSettings> {
  const res = await fetch('/api/admin/business/notifications')
  if (!res.ok) throw await failure(res, 'Could not load the notification settings')
  return res.json()
}

export async function updateNotificationSettings(
  change: NotificationSettingsChange,
): Promise<NotificationSettings> {
  const res = await send('PATCH', '/api/admin/business/notifications', change)
  if (!res.ok) throw await failure(res, 'Could not save the notification settings')
  return res.json()
}

/** Calls the real adapter synchronously and, only on success, sets the verified timestamp —
 *  the one thing that ungates the "disabled behind a banner" state for real (#11). */
export async function sendTestEmail(to: string): Promise<NotificationSettings> {
  const res = await send('POST', '/api/admin/business/notifications/test-email', { to })
  if (!res.ok) throw await failure(res, 'The test email could not be sent')
  return res.json()
}

/** SMS has no verified flag to flip (`notifications/providers.py::sms_ready` never gates on
 *  one) — this still performs a real send, it just reports success or the adapter's error. */
export async function sendTestSms(to: string): Promise<{ sent: boolean }> {
  const res = await send('POST', '/api/admin/business/notifications/test-sms', { to })
  if (!res.ok) throw await failure(res, 'The test text could not be sent')
  return res.json()
}

export async function updateNotificationTemplate(
  id: string,
  change: { subject_template: string | null; body_template: string },
): Promise<NotificationTemplate> {
  const res = await send('PUT', `/api/admin/business/notifications/templates/${id}`, change)
  if (!res.ok) throw await failure(res, 'Could not save the template')
  return res.json()
}

// --- services: the catalog ----------------------------------------------------------------

/** One thing delivering a service needs. `resource_id` null is "any active resource of this
 *  kind" — the majority case, and the reason `kind` is on the row at all. */
export type ServiceRequirement = {
  kind: ResourceKind
  resource_id: string | null
  /** Carried by the server so a screen with no resource list still has something to print. */
  resource_name: string | null
  /**
   * Whether that resource is still bookable — null for an "any" row, which is a question
   * about a kind rather than about one resource. A named requirement survives its resource
   * being deactivated, flagged rather than dropped: a service that quietly lost its only
   * laser would read as needing nothing at all.
   */
  resource_active: boolean | null
}

/**
 * A service: what the business sells. `duration_minutes` plus the two buffers is the width
 * the availability engine slides across a staff member's day, and `requirements` is what its
 * free intervals have to be intersected with.
 *
 * `price_cents` is an integer — money is cents everywhere (CLAUDE.md). The forms show dollars
 * and convert; nothing here ever holds a float.
 *
 * Editing a service never touches an appointment already booked against it: booking copies
 * the duration, the buffers and the price onto the appointment at the moment it is made.
 */
export type ServiceRow = {
  id: string
  name: string
  description: string | null
  duration_minutes: number
  buffer_before_minutes: number
  buffer_after_minutes: number
  price_cents: number
  /** Tax component codes this service toggles on, and how its price is entered. */
  tax_component_keys: string[]
  tax_convention: TaxConvention
  /** False is "staff may book it, the public portal may not offer it". */
  bookable_online: boolean
  active: boolean
  sort_order: number
  /** Staff ids, not user ids — the same id `/api/admin/staff` rows carry. */
  staff_ids: string[]
  requirements: ServiceRequirement[]
}

/** What the create and edit forms send. The two sets are their own endpoints. */
export type ServiceDraft = {
  name: string
  description: string | null
  duration_minutes: number
  buffer_before_minutes: number
  buffer_after_minutes: number
  price_cents: number
  tax_component_keys: string[]
  tax_convention: TaxConvention
  bookable_online: boolean
  sort_order: number
}

export async function fetchServices(includeInactive = false): Promise<ServiceRow[]> {
  const res = await fetch(`/api/admin/services${includeInactive ? '?include_inactive=true' : ''}`)
  if (!res.ok) throw await failure(res, 'Could not load the services')
  return (await res.json()).services
}

export async function createService(draft: ServiceDraft): Promise<ServiceRow> {
  const res = await send('POST', '/api/admin/services', draft)
  if (!res.ok) throw await failure(res, 'Could not create the service')
  return res.json()
}

export async function updateService(
  id: string,
  draft: Partial<ServiceDraft>,
): Promise<ServiceRow> {
  const res = await send('PATCH', `/api/admin/services/${id}`, draft)
  if (!res.ok) throw await failure(res, 'Could not save the service')
  return res.json()
}

/** The whole eligible set, replaced. Leaving somebody out is how they are dropped. */
export async function replaceServiceStaff(id: string, staffIds: string[]): Promise<ServiceRow> {
  const res = await send('PUT', `/api/admin/services/${id}/staff`, { staff_ids: staffIds })
  if (!res.ok) throw await failure(res, 'Could not save who may deliver this')
  return res.json()
}

/** The whole requirement set, replaced. `resource_name` is the server's to fill in. */
export async function replaceServiceRequirements(
  id: string,
  requirements: { kind: ResourceKind; resource_id: string | null }[],
): Promise<ServiceRow> {
  const res = await send('PUT', `/api/admin/services/${id}/requirements`, { requirements })
  if (!res.ok) throw await failure(res, 'Could not save what this service needs')
  return res.json()
}

export async function deactivateService(id: string): Promise<ServiceRow> {
  const res = await send('POST', `/api/admin/services/${id}/deactivate`, {})
  if (!res.ok) throw await failure(res, 'Could not deactivate the service')
  return res.json()
}

export async function reactivateService(id: string): Promise<ServiceRow> {
  const res = await send('POST', `/api/admin/services/${id}/reactivate`, {})
  if (!res.ok) throw await failure(res, 'Could not reactivate the service')
  return res.json()
}

// --- products: the retail catalog (M4 #56) ------------------------------------------------

/** One sellable unit — a SKU, an optional barcode, a price and a whole-unit stock count,
 *  with its own low-stock threshold independent of any sibling variant. Unlike a service's
 *  eligible-staff set, a variant has identity of its own: it is created, edited, deactivated
 *  and reactivated one at a time, never as part of a bulk save. */
export type ProductVariantRow = {
  id: string
  product_id: string
  name: string
  sku: string
  barcode: string | null
  price_cents: number
  quantity_on_hand: number
  low_stock_threshold: number
  tax_component_keys: string[]
  tax_convention: TaxConvention
  active: boolean
  sort_order: number
}

export type ProductRow = {
  id: string
  name: string
  description: string | null
  active: boolean
  sort_order: number
  variants: ProductVariantRow[]
}

export type ProductDraft = { name: string; description: string | null; sort_order: number }

export type ProductVariantDraft = {
  name: string
  sku: string
  barcode: string | null
  price_cents: number
  low_stock_threshold: number
  tax_component_keys: string[]
  tax_convention: TaxConvention
}

export async function fetchProducts(includeInactive = false): Promise<ProductRow[]> {
  const res = await fetch(`/api/admin/products${includeInactive ? '?include_inactive=true' : ''}`)
  if (!res.ok) throw await failure(res, 'Could not load the products')
  return (await res.json()).products
}

export async function createProduct(draft: ProductDraft): Promise<ProductRow> {
  const res = await send('POST', '/api/admin/products', draft)
  if (!res.ok) throw await failure(res, 'Could not create the product')
  return res.json()
}

export async function updateProduct(
  id: string,
  draft: Partial<ProductDraft>,
): Promise<ProductRow> {
  const res = await send('PATCH', `/api/admin/products/${id}`, draft)
  if (!res.ok) throw await failure(res, 'Could not save the product')
  return res.json()
}

export async function deactivateProduct(id: string): Promise<ProductRow> {
  const res = await send('POST', `/api/admin/products/${id}/deactivate`, {})
  if (!res.ok) throw await failure(res, 'Could not deactivate the product')
  return res.json()
}

export async function reactivateProduct(id: string): Promise<ProductRow> {
  const res = await send('POST', `/api/admin/products/${id}/reactivate`, {})
  if (!res.ok) throw await failure(res, 'Could not reactivate the product')
  return res.json()
}

/** Every variant endpoint returns the whole product — the same shape the table reads — so a
 *  screen that just created or edited one variant never has to refetch separately. */
/** A delivery arriving (`inventory.receive`): the only way stock goes up outside a return. */
export async function receiveStock(
  productId: string,
  variantId: string,
  quantity: number,
  reason: string | null,
): Promise<ProductRow> {
  const res = await send('POST', `/api/admin/products/${productId}/variants/${variantId}/receive`, {
    quantity,
    reason,
  })
  if (!res.ok) throw await failure(res, 'Could not receive the stock')
  return res.json()
}

/** A counted correction (`inventory.adjust`): `delta` is signed, already computed from the
 *  counted quantity against on-hand — the server records exactly what is sent. */
export async function adjustStock(
  productId: string,
  variantId: string,
  quantityDelta: number,
  reason: string,
): Promise<ProductRow> {
  const res = await send('POST', `/api/admin/products/${productId}/variants/${variantId}/adjust`, {
    quantity_delta: quantityDelta,
    reason,
  })
  if (!res.ok) throw await failure(res, 'Could not adjust the stock')
  return res.json()
}

export async function createVariant(
  productId: string,
  draft: ProductVariantDraft,
): Promise<ProductRow> {
  const res = await send('POST', `/api/admin/products/${productId}/variants`, draft)
  if (!res.ok) throw await failure(res, 'Could not create the variant')
  return res.json()
}

export async function updateVariant(
  productId: string,
  variantId: string,
  draft: Partial<ProductVariantDraft>,
): Promise<ProductRow> {
  const res = await send('PATCH', `/api/admin/products/${productId}/variants/${variantId}`, draft)
  if (!res.ok) throw await failure(res, 'Could not save the variant')
  return res.json()
}

export async function deactivateVariant(productId: string, variantId: string): Promise<ProductRow> {
  const res = await send(
    'POST',
    `/api/admin/products/${productId}/variants/${variantId}/deactivate`,
    {},
  )
  if (!res.ok) throw await failure(res, 'Could not deactivate the variant')
  return res.json()
}

export async function reactivateVariant(productId: string, variantId: string): Promise<ProductRow> {
  const res = await send(
    'POST',
    `/api/admin/products/${productId}/variants/${variantId}/reactivate`,
    {},
  )
  if (!res.ok) throw await failure(res, 'Could not reactivate the variant')
  return res.json()
}

// --- packages: prepaid credit definitions (Settings → Packages, #101) ----------------------

/** One service a package definition carries credits for. `service_active` is carried the
 *  same way `ServiceRequirement.resource_active` is — a definition that already named a
 *  service which has since been deactivated has to say so rather than go quietly wrong. */
export type PackageDefinitionServiceRow = {
  service_id: string
  service_name: string
  service_active: boolean
  credits: number
}

/** A package or bundle definition: what it costs, how long a purchase is good for, and the
 *  services and credit counts it carries. `services` is never empty — see
 *  `billing/packages.py`'s module docstring for why a definition with none is not
 *  representable at all, not just an edge case of a real one. */
export type PackageDefinitionRow = {
  id: string
  name: string
  description: string | null
  price_cents: number
  /** Null: never expires. Otherwise, days from the purchase date. */
  expires_after_days: number | null
  transferable: boolean
  tax_component_keys: string[]
  tax_convention: TaxConvention
  active: boolean
  services: PackageDefinitionServiceRow[]
}

export type PackageDefinitionServiceDraft = { service_id: string; credits: number }

/** The scalar fields, on both create and edit. `services` is its own field on create (the
 *  whole set, required) and its own endpoint on edit (`PUT /{id}/services`) — the same split
 *  `ServiceDraft` keeps for eligible staff and requirements. */
export type PackageDefinitionDraft = {
  name: string
  description: string | null
  price_cents: number
  expires_after_days: number | null
  transferable: boolean
  tax_component_keys: string[]
  tax_convention: TaxConvention
}

export async function fetchPackageDefinitions(
  includeInactive = false,
): Promise<PackageDefinitionRow[]> {
  const res = await fetch(`/api/admin/packages${includeInactive ? '?include_inactive=true' : ''}`)
  if (!res.ok) throw await failure(res, 'Could not load the packages')
  return (await res.json()).packages
}

export async function createPackageDefinition(
  draft: PackageDefinitionDraft & { services: PackageDefinitionServiceDraft[] },
): Promise<PackageDefinitionRow> {
  const res = await send('POST', '/api/admin/packages', draft)
  if (!res.ok) throw await failure(res, 'Could not create the package')
  return res.json()
}

export async function updatePackageDefinition(
  id: string,
  draft: Partial<PackageDefinitionDraft>,
): Promise<PackageDefinitionRow> {
  const res = await send('PATCH', `/api/admin/packages/${id}`, draft)
  if (!res.ok) throw await failure(res, 'Could not save the package')
  return res.json()
}

/** The whole services set, replaced — the same "nothing half-saved" shape
 *  `replaceServiceRequirements` uses. */
export async function replacePackageDefinitionServices(
  id: string,
  services: PackageDefinitionServiceDraft[],
): Promise<PackageDefinitionRow> {
  const res = await send('PUT', `/api/admin/packages/${id}/services`, { services })
  if (!res.ok) throw await failure(res, "Could not save the package's services")
  return res.json()
}

export async function deactivatePackageDefinition(id: string): Promise<PackageDefinitionRow> {
  const res = await send('POST', `/api/admin/packages/${id}/deactivate`, {})
  if (!res.ok) throw await failure(res, 'Could not deactivate the package')
  return res.json()
}

export async function reactivatePackageDefinition(id: string): Promise<PackageDefinitionRow> {
  const res = await send('POST', `/api/admin/packages/${id}/reactivate`, {})
  if (!res.ok) throw await failure(res, 'Could not reactivate the package')
  return res.json()
}

// --- forms: templates and their frozen versions (Settings → Forms) ---------------------------

export type FormKind = 'intake' | 'consent' | 'waiver' | 'other'

/** What the next publish copies: the schema and the two versioned flags. */
export type FormDraft = {
  schema: FormSchema
  is_health_form: boolean
  is_mandatory: boolean
}

export type FormTemplate = {
  id: string
  name: string
  kind: FormKind
  retired_at: string | null
  updated_at: string
  latest_version: number | null
  has_unpublished_changes: boolean
  draft: FormDraft
  /** Task 8 (#51): identity-level compliance settings, edited outside versioning. */
  applies_to_all: boolean
  valid_for_months: number | null
  service_ids: string[]
}

export type FormVersionSummary = {
  number: number
  /** The title and kind as published — what the client saw. The template's may since differ. */
  name: string
  kind: FormKind
  published_at: string
  requires_resignature: boolean
  is_health_form: boolean
  is_mandatory: boolean
}

export async function fetchFormTemplates(): Promise<FormTemplate[]> {
  const res = await fetch('/api/admin/forms')
  if (!res.ok) throw await failure(res, 'Could not load the forms')
  return (await res.json()).templates
}

export async function createFormTemplate(draft: {
  name: string
  kind: FormKind
  is_health_form: boolean
  is_mandatory: boolean
}): Promise<FormTemplate> {
  const res = await send('POST', '/api/admin/forms', draft)
  if (!res.ok) throw await failure(res, 'Could not create the form')
  return res.json()
}

/** The whole draft, replaced. Keys go back exactly as the builder minted them. */
export async function saveFormDraft(
  id: string,
  draft: FormDraft & { name: string; kind: FormKind },
): Promise<FormTemplate> {
  const res = await send('PUT', `/api/admin/forms/${id}/draft`, draft)
  if (!res.ok) throw await failure(res, 'Could not save the draft')
  return res.json()
}

export async function publishFormTemplate(
  id: string,
  requiresResignature: boolean,
): Promise<FormVersionSummary> {
  const res = await send('POST', `/api/admin/forms/${id}/publish`, {
    requires_resignature: requiresResignature,
  })
  if (!res.ok) throw await failure(res, 'Could not publish the form')
  return res.json()
}

export async function fetchFormVersions(id: string): Promise<FormVersionSummary[]> {
  const res = await fetch(`/api/admin/forms/${id}/versions`)
  if (!res.ok) throw await failure(res, 'Could not load the version history')
  return (await res.json()).versions
}

/** A published version's own fields — the summary list has everything but these. Used only
 *  to tell whether the current draft is unchanged from the latest published version (fix:
 *  Publish must not offer a version identical to the one already published). */
export type FormVersion = FormVersionSummary & { schema: FormSchema }

export async function fetchFormVersion(id: string, number: number): Promise<FormVersion> {
  const res = await fetch(`/api/admin/forms/${id}/versions/${number}`)
  if (!res.ok) throw await failure(res, 'Could not load the version')
  return res.json()
}

/** Task 8 (#51): who the essential-forms checklist expects this form from — every client, or
 *  clients with an upcoming appointment for one of these services — and for how long a
 *  submission stays valid. Identity-level: `PUT` here never touches the draft or a version. */
export type FormTemplateSettings = {
  applies_to_all: boolean
  valid_for_months: number | null
  service_ids: string[]
}

export async function saveFormTemplateSettings(
  id: string,
  settings: FormTemplateSettings,
): Promise<FormTemplate> {
  const res = await send('PUT', `/api/admin/forms/${id}/settings`, settings)
  if (!res.ok) throw await failure(res, 'Could not save the settings')
  return res.json()
}

export async function retireFormTemplate(id: string): Promise<FormTemplate> {
  const res = await send('POST', `/api/admin/forms/${id}/retire`, {})
  if (!res.ok) throw await failure(res, 'Could not retire the form')
  return res.json()
}

export async function deleteFormTemplate(id: string): Promise<void> {
  const res = await send('DELETE', `/api/admin/forms/${id}`)
  if (!res.ok) throw await failure(res, 'Could not delete the form')
}

// --- forms: sending a link to a client, and the page the client opens -----------------------

/** A form the front desk may send: published, not retired, at its latest version. */
export type SendableForm = { template_id: string; name: string; kind: FormKind; version: number }

/** The one answer that carries the link's URL. Never stored or refetched: gone on close. */
export type IssuedFormLink = {
  id: string
  url: string
  expires_at: string
  /** The address the link was queued to, or null when the client has none on file. */
  emailed_to: string | null
}

export type OpenFormLink = {
  id: string
  template_name: string
  version: number
  issued_at: string
  expires_at: string
}

/** `forms.issue`, Staff Mode. */
export async function fetchSendableForms(): Promise<SendableForm[]> {
  const res = await fetch('/api/forms/templates')
  if (!res.ok) throw await failure(res, 'Could not load the forms')
  return (await res.json()).templates
}

export async function issueFormLink(customerId: string, templateId: string): Promise<IssuedFormLink> {
  const res = await post(`/api/customers/${customerId}/form-links`, { template_id: templateId })
  if (!res.ok) throw await failure(res, 'Could not send the form')
  return res.json()
}

export async function fetchOpenFormLinks(customerId: string): Promise<OpenFormLink[]> {
  const res = await fetch(`/api/customers/${customerId}/form-links`)
  if (!res.ok) throw await failure(res, 'Could not load the sent forms')
  return (await res.json()).links
}

export async function revokeFormLink(customerId: string, linkId: string): Promise<void> {
  const res = await post(`/api/customers/${customerId}/form-links/${linkId}/revoke`, {})
  if (!res.ok) throw await failure(res, 'Could not revoke the link')
}

/** What `/f/#<token>` renders: the pinned version, the business, and a first name. */
export type PublicForm = {
  version_id: string
  template_name: string
  schema: FormSchema
  business: { name: string; logo_url: string | null }
  client_first_name: string
  expires_at: string
}

/**
 * The public lookup. The token goes in a JSON body, never the path: a path is what every
 * access log writes down. `credentials: 'omit'`: a staff browser opening a client's link must
 * not send its session along. `referrerPolicy: 'origin'` overrides the page's `no-referrer`
 * for this one request, which would otherwise make the browser send `Origin: null` — and the
 * server accepts this POST only from this deployment's origin. No path is sent either way.
 */
export async function fetchPublicForm(token: string): Promise<PublicForm> {
  const res = await fetch('/api/public/forms/lookup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
    credentials: 'omit',
    referrerPolicy: 'origin',
    cache: 'no-store',
  })
  if (!res.ok) throw await failure(res, 'Could not open the form')
  return res.json()
}

/** What the page sends. `submission_id` is minted once per page and resent on every retry. */
export type FormSubmitPayload = {
  token: string
  submission_id: string
  version_id: string
  answers: Answers
}

/**
 * The public submit (#47), sent exactly like the lookup: token in the body, no credentials,
 * `Origin` kept. 200 `received` or `already_received` (a retry of what was filed); a 422
 * `invalid_answers` carries `errors: {key: code}` on the thrown error's body.
 */
export async function submitPublicForm(payload: FormSubmitPayload): Promise<{ status: string }> {
  const res = await fetch('/api/public/forms/submit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    credentials: 'omit',
    referrerPolicy: 'origin',
    cache: 'no-store',
  })
  if (!res.ok) throw await failure(res, 'Could not send the form')
  return res.json()
}

/** One completed form, as the profile lists it: metadata, never answers. */
export type FormSubmissionSummary = {
  id: string
  template_id: string
  template_name: string
  version: number
  method: 'link' | 'scan'
  submitted_at: string
  pdf_ready: boolean
}

export async function fetchFormPdf(customerId: string, id: string): Promise<Blob> {
  const res = await fetch(`/api/customers/${customerId}/forms/${id}/pdf`, { cache: 'no-store' })
  if (!res.ok || res.status === 202) throw await failure(res, 'The PDF is still rendering. Try again shortly.')
  return res.blob()
}

export async function fetchPaperVersions(templateId: string): Promise<FormVersionSummary[]> {
  const res = await fetch(`/api/forms/templates/${templateId}/versions`)
  if (!res.ok) throw await failure(res, 'Could not load published versions')
  return (await res.json()).versions
}

export async function fetchBlankForm(templateId: string, version: number): Promise<string> {
  const res = await fetch(`/api/admin/forms/${templateId}/versions/${version}/print`, { cache: 'no-store' })
  if (!res.ok) throw await failure(res, 'Could not print the form')
  return res.text()
}

export async function uploadFormScan(customerId: string, payload: {
  submission_id: string; template_id: string; version_number: number; pages: string[]
}): Promise<void> {
  const res = await send('POST', `/api/customers/${customerId}/forms/scans`, payload)
  if (!res.ok) throw await failure(res, 'Could not save the scan')
}

/** One completed form opened (`forms.view`; logged): its own version's fields, and answers. */
export type FormSubmissionDetail = FormSubmissionSummary & {
  fields: { key: string; type: FieldType; label: string }[]
  answers: Answers
}

export async function fetchClientSubmissions(customerId: string): Promise<FormSubmissionSummary[]> {
  const res = await fetch(`/api/customers/${customerId}/forms`)
  if (!res.ok) throw await failure(res, 'Could not load the completed forms')
  return (await res.json()).submissions
}

export async function fetchFormSubmission(customerId: string, id: string): Promise<FormSubmissionDetail> {
  const res = await fetch(`/api/customers/${customerId}/forms/${id}`)
  if (!res.ok) throw await failure(res, 'Could not open the form')
  return res.json()
}

// --- forms: essential-forms compliance (Task 8, #51) ------------------------------------------

export type ComplianceStatus = 'missing' | 'expired' | 'resign_required'

/** One essential template a client is not currently compliant with. Metadata — a name and a
 *  status, never an answer (`core.access_log.PHI_FIELDS`) — so neither endpoint below logs. */
export type ComplianceEntry = { template_id: string; name: string; status: ComplianceStatus }

/** `customers.view`. The profile's alert banner. */
export async function fetchCustomerCompliance(customerId: string): Promise<ComplianceEntry[]> {
  const res = await fetch(`/api/customers/${customerId}/compliance`)
  if (!res.ok) throw await failure(res, 'Could not load the compliance status')
  return (await res.json()).templates
}

/** One client on the "Forms needed" dashboard, with the appointment that put them there. */
export type FormsNeededEntry = {
  customer_id: string
  customer_name: string
  next_appointment_at: string
  templates: ComplianceEntry[]
}

/** `forms.issue`. Clients with a confirmed appointment in the next `days` (≤ 60, default 14)
 *  who have any non-compliant essential form. */
export async function fetchFormsNeeded(days = 14): Promise<FormsNeededEntry[]> {
  const res = await fetch(`/api/forms/compliance?${new URLSearchParams({ days: String(days) })}`)
  if (!res.ok) throw await failure(res, 'Could not load the forms-needed list')
  return (await res.json()).clients
}

// --- availability: the bookable slots -------------------------------------------------------

/** One start that can be booked. Instants in UTC (`...Z`); `staff_ids` is everyone eligible
 *  who is free for it, so "any available provider" is the slot and a person is the choice. */
export type AvailabilitySlot = {
  starts_at: string
  /** The appointment the client sees — the duration, without the turnaround either side. */
  ends_at: string
  staff_ids: string[]
}

export type AvailabilityDay = {
  /** A business-local date, `YYYY-MM-DD`. */
  date: string
  slots: AvailabilitySlot[]
}

export type Availability = {
  service_id: string
  timezone: string
  granularity_minutes: number
  /** The last local date anything is computed for. Days after it come back with no slots. */
  horizon_ends_on: string
  /** Every date asked for, in order, empty when nothing can be booked. */
  days: AvailabilityDay[]
}

/**
 * `from` and `to` are business-local dates, `to` inclusive, at most 31 days apart. A 409 is a
 * service the catalog says cannot be booked yet; its `body.unbookable_reasons` say why.
 */
export async function fetchAvailability(query: {
  service_id: string
  from: string
  to: string
  staff_id?: string
}): Promise<Availability> {
  const params = new URLSearchParams({ service_id: query.service_id, from: query.from, to: query.to })
  if (query.staff_id) params.set('staff_id', query.staff_id)
  const res = await fetch(`/api/availability?${params}`)
  if (!res.ok) throw await failure(res, 'Could not load the available times')
  return res.json()
}

// --- the schedule: who has a column, what is booked, and booking one -----------------------

/** A column on the schedule. Active staff only, in column order, with the colour's hexes
 *  so a screen holding no `users.manage` can paint without the admin palette. */
export type RosterEntry = {
  id: string
  display_name: string
  colour: string
  hex: string
  dark_hex: string
  sort_order: number
  /** The account behind the column: how this screen tells its own column from the others,
   *  which is the line between overriding one's own evening and somebody else's. */
  user_id: string
}

export async function fetchRoster(): Promise<RosterEntry[]> {
  const res = await fetch('/api/staff')
  if (!res.ok) throw await failure(res, 'Could not load the staff')
  return (await res.json()).staff
}

/** The catalog as the booking screen and the availability engine read it: active services,
 *  with `staff_ids` already pruned to active staff and `bookable` decided server-side. */
export type CatalogService = Omit<ServiceRow, 'active'> & {
  bookable: boolean
  unbookable_reasons: string[]
}

export async function fetchCatalog(): Promise<CatalogService[]> {
  const res = await fetch('/api/catalog/services')
  if (!res.ok) throw await failure(res, 'Could not load the services')
  return (await res.json()).services
}

/** Derived at read time from completed-appointment counts (never stored, never PHI) — see
 *  `vip_visit_threshold` on the business profile. Carried on every row the server sends,
 *  including the booking dialog's search results. */
export type Classification = 'new' | 'repeat' | 'vip'

export type Customer = {
  id: string
  first_name: string
  last_name: string
  email: string | null
  phone: string | null
  classification: Classification
}

export type CustomerDraft = {
  first_name: string
  last_name: string
  email?: string | null
  phone?: string | null
}

/** Prefix matches on either name, the email, or the digits of a phone number. The booking
 *  dialog's picker: the first page is all it wants. */
export async function searchCustomers(q: string): Promise<Customer[]> {
  const res = await fetch(`/api/customers?${new URLSearchParams({ q })}`)
  if (!res.ok) throw await failure(res, 'Could not search the customers')
  return (await res.json()).customers
}

/** `customers.manage`. The same endpoint the booking dialog's inline "new client" posts to —
 *  one place a customer is made, whichever screen starts it. */
export async function createCustomer(draft: CustomerDraft): Promise<Customer> {
  const res = await send('POST', '/api/customers', draft)
  if (!res.ok) throw await failure(res, 'Could not create the client')
  return res.json()
}

/** A customer as the Clients list and profile show one: the picker's fields plus when the
 *  record was made. */
export type CustomerRecord = Customer & { created_at: string }

export type CustomerList = { customers: CustomerRecord[]; total: number }

/** The Clients list: alphabetical by last name, a page at a time (`page_size` ≤ 100), with
 *  the total; `q` narrows it the same way `searchCustomers` does. Never logged as an access
 *  on the server (ADR-0002 §4) — opening a profile is. */
export async function fetchCustomers(query: {
  q?: string
  page?: number
  page_size?: number
}): Promise<CustomerList> {
  const params = new URLSearchParams()
  if (query.q) params.set('q', query.q)
  if (query.page) params.set('page', String(query.page))
  if (query.page_size) params.set('page_size', String(query.page_size))
  const res = await fetch(`/api/customers?${params}`)
  if (!res.ok) throw await failure(res, 'Could not load the clients')
  return res.json()
}

/** One row of a client's history. Instants are UTC; `timezone` on the profile is what to
 *  print them in. */
export type Visit = {
  id: string
  starts_at: string
  ends_at: string
  status: Appointment['status']
  booking_group_id: string | null
  service: { id: string; name: string }
  staff: { id: string; display_name: string; colour: string }
}

/** The profile's own fields, beyond what the list and the search carry — DOB, both
 *  contacts, front-desk notes. PHI: only the profile `GET` and the `PATCH` that edited it
 *  ever send these; the list and the booking dialog's search never do. */
export type CustomerDetail = CustomerRecord & {
  date_of_birth: string | null
  emergency_contact_name: string | null
  emergency_contact_phone: string | null
  emergency_contact_relationship: string | null
  secondary_contact_name: string | null
  secondary_contact_phone: string | null
  secondary_contact_email: string | null
  notes: string | null
  updated_at: string
  /** When this chart may be destroyed (ADR-0001). `expires_on` is a business-local date,
   *  for `held` (ends on) and `expired` (ended on — no longer held); `needs_dob` is held
   *  indefinitely until a date of birth is added. */
  retention: { status: 'held' | 'expired' | 'not_held' | 'needs_dob'; expires_on: string | null }
  /** Erasure was requested: hidden from the list, search and booking. */
  suppressed: boolean
  /** What was kept and why — null until an erasure is requested. */
  erasure: Erasure | null
}

/** An erasure request's outcome (ADR-0001 §3). `retained` is empty when nothing is held;
 *  `held_until` is a business-local date, null for an indefinite hold (no DOB on file). */
export type Erasure = {
  id: string
  requested_at: string
  held: boolean
  held_until: string | null
  held_reason: string | null
  retained: string[]
  purged_at: string | null
}

/** A terminal delivery failure (Phase 12 Task 4, #11): a permanent one (a bad address/number,
 *  rejected credentials) writes this row and stops retrying — a transient one (network error,
 *  a 5xx) just keeps retrying in the background and never appears here. */
export type NotificationFailure = {
  id: string
  channel: 'email' | 'sms'
  notification_type: string
  recipient: string
  reason: string
  occurred_at: string
}

export type CustomerProfile = {
  customer: CustomerDetail
  timezone: string
  /** Newest first, upcoming included, cancelled and no-shows too. */
  appointments: Visit[]
  /** Newest first. Empty for the common case — nothing has ever permanently failed to send. */
  notification_failures: NotificationFailure[]
}

/** The profile and its visits in one response — one request, because on the server it is
 *  exactly one PHI access-log row. Every call is an audited access: make it on purpose. */
export async function fetchCustomerProfile(id: string): Promise<CustomerProfile> {
  const res = await fetch(`/api/customers/${encodeURIComponent(id)}`)
  if (!res.ok) throw await failure(res, 'Could not load this client')
  return res.json()
}

/** One open of a client's record, as the access log holds it: identifiers only, never
 *  what was seen. `actor_name` is resolved at read time and falls back to the id. */
export type AccessEntry = {
  id: number
  occurred_at: string
  actor_user_id: string
  actor_name: string
  actor_role: string
  resource_type: string
  resource_id: string
  action: string
  ip: string | null
}

/** A page of the report. `from`/`to` are the business-local dates actually applied — the
 *  server's 90-day default when none were sent. */
export type AccessReport = {
  entries: AccessEntry[]
  total: number
  from: string
  to: string
  timezone: string
}

/** Admin Mode + `audit.view`. Reading it is not itself an access-log event. */
export async function fetchAccessLog(
  id: string,
  query: { from?: string; to?: string; page?: number; page_size?: number },
): Promise<AccessReport> {
  const params = new URLSearchParams()
  if (query.from) params.set('from', query.from)
  if (query.to) params.set('to', query.to)
  if (query.page) params.set('page', String(query.page))
  if (query.page_size) params.set('page_size', String(query.page_size))
  const res = await fetch(`/api/admin/customers/${encodeURIComponent(id)}/access-log?${params}`)
  if (!res.ok) throw await failure(res, 'Could not load the access history')
  return res.json()
}

// --- CSV exports (#86/#95's shared mechanism): request, poll, download -----------------

/** The one shape every export kind's request/poll endpoint answers in
 *  (`core/exports.py`'s `ReportExport`, as `access_log`/`commission`/`package_liability`
 *  each serialise it) — `<ExportControl>` (`components/export-control.tsx`) knows only this
 *  shape, never a report's own params. */
export type ExportJob = {
  id: string
  status: 'pending' | 'ready' | 'failed'
  created_at: string
  completed_at: string | null
  download_url: string | null
}

/** Fetches a ready export and saves it under the filename the server chose
 *  (`Content-Disposition`, never guessed client-side) — the one download mechanics every
 *  export kind shares. Rejects with the same `ApiError` a 410/409 always throws. */
async function downloadExportFile(url: string): Promise<void> {
  const res = await fetch(url, { cache: 'no-store' })
  if (!res.ok) throw await failure(res, 'Could not download the export')
  const filename = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') ?? '')?.[1] ?? 'export.csv'
  const blobUrl = URL.createObjectURL(await res.blob())
  const link = document.createElement('a')
  link.href = blobUrl
  link.download = filename
  link.click()
  URL.revokeObjectURL(blobUrl)
}

/** Requests the client access-log CSV (#87) for the panel's own range — `audit.view`, Admin
 *  Mode. Polled with `fetchAccessLogExportStatus`, downloaded with `downloadAccessLogExport`. */
export async function requestAccessLogExport(
  customerId: string,
  query: { from?: string; to?: string },
): Promise<ExportJob> {
  const res = await post(
    `/api/admin/customers/${encodeURIComponent(customerId)}/access-log/exports`,
    { from: query.from, to: query.to },
  )
  if (!res.ok) throw await failure(res, 'Could not request the export')
  return res.json()
}

export async function fetchAccessLogExportStatus(customerId: string, exportId: string): Promise<ExportJob> {
  const res = await fetch(
    `/api/admin/customers/${encodeURIComponent(customerId)}/access-log/exports/${encodeURIComponent(exportId)}`,
  )
  if (!res.ok) throw await failure(res, 'Could not check the export')
  return res.json()
}

export function downloadAccessLogExport(customerId: string, exportId: string): Promise<void> {
  return downloadExportFile(
    `/api/admin/customers/${encodeURIComponent(customerId)}/access-log/exports/${encodeURIComponent(exportId)}/csv`,
  )
}

/** Every field the edit dialog can send. A key left out of the object is left alone on the
 *  server — never sent as `null` by accident — which is what lets the dialog submit only
 *  what actually changed. */
export type CustomerPatch = Partial<{
  first_name: string
  last_name: string
  email: string | null
  phone: string | null
  date_of_birth: string | null
  emergency_contact_name: string | null
  emergency_contact_phone: string | null
  emergency_contact_relationship: string | null
  secondary_contact_name: string | null
  secondary_contact_phone: string | null
  secondary_contact_email: string | null
  notes: string | null
}>

/** Not a PHI access in its own right, and its response carries none: the server sends back
 *  `CustomerRecord` — the same non-PHI shape the list uses — never the DOB, the contacts or
 *  the notes, so there is nothing here for `LogAccess` to have missed. Invalidate the
 *  profile query afterwards; that refetch is the one PHI read, and it does log. */
export async function updateCustomer(id: string, patch: CustomerPatch): Promise<CustomerRecord> {
  const res = await send('PATCH', `/api/customers/${encodeURIComponent(id)}`, patch)
  if (!res.ok) throw await failure(res, 'Could not save this client')
  return res.json()
}

/** Admin Mode + `customers.erase`. Irreversible: removes what is not held at once, and the
 *  answer says what was kept and why. Its response is a PHI access on the server. */
export async function requestErasure(id: string, note: string | null): Promise<Erasure> {
  const res = await post(`/api/customers/${encodeURIComponent(id)}/erasure`, { note })
  if (!res.ok) throw await failure(res, 'Could not erase this client')
  return res.json()
}

/**
 * One appointment as the calendar draws it. The numbers are the *snapshot* taken from the
 * service at booking — editing the catalog afterwards changes none of them. Instants are UTC.
 */
export type Appointment = {
  id: string
  status: 'confirmed' | 'completed' | 'cancelled' | 'no_show'
  starts_at: string
  ends_at: string
  duration_minutes: number
  buffer_before_minutes: number
  buffer_after_minutes: number
  price_cents: number
  notes: string | null
  booking_group_id: string | null
  /** When each terminal state was reached — null until it is. `completed_at` is the event
   *  later phases (treatment receipts, package credits, commission) key off. */
  completed_at: string | null
  cancelled_at: string | null
  cancel_reason: string | null
  no_show_at: string | null
  /** The advisory rules this was confirmed past, and why — null when none were. */
  overridden_rules: OverrideRule[] | null
  override_reason: string | null
  customer: Customer
  service: { id: string; name: string }
  staff: { id: string; display_name: string; colour: string }
  resources: { id: string; name: string; kind: ResourceKind }[]
}

/** `from`/`to` are business-local dates, `to` inclusive, at most 31 days apart. Cancelled
 *  appointments are left out unless `include_cancelled` — the calendar's toggle. */
export async function fetchAppointments(query: {
  from: string
  to: string
  staff_id?: string
  include_cancelled?: boolean
}): Promise<{ timezone: string; appointments: Appointment[] }> {
  const params = new URLSearchParams({ from: query.from, to: query.to })
  if (query.staff_id) params.set('staff_id', query.staff_id)
  if (query.include_cancelled) params.set('include_cancelled', 'true')
  const res = await fetch(`/api/appointments?${params}`)
  if (!res.ok) throw await failure(res, 'Could not load the appointments')
  return res.json()
}

/**
 * The status lifecycle (Task 18): `confirmed → completed | cancelled | no_show`, every one
 * terminal — no reopen in M1. Each is its own request rather than a generic "set status" so
 * the network tab already says what happened. A 409 `invalid_transition` is an appointment
 * that is not `confirmed` any more; a no-show attempted before `starts_at` is 422
 * `not_yet_started`.
 */
export async function completeAppointment(
  id: string,
  packagePurchaseId: string | null = null,
): Promise<Appointment> {
  const res = await send('POST', `/api/appointments/${id}/complete`, {
    package_purchase_id: packagePurchaseId,
  })
  if (!res.ok) throw await failure(res, 'Could not complete the appointment')
  return res.json()
}

/** One paid, unexpired, unrefunded package of this client's that still covers the
 *  appointment's service (#72). `value_cents` is what the next session would be worth. */
export type PackageCredit = {
  package_purchase_id: string
  name: string
  purchased_at: string
  expires_at: string | null
  credits_total: number
  credits_remaining: number
  value_cents: number
}

export async function fetchPackageCredits(appointmentId: string): Promise<PackageCredit[]> {
  const res = await fetch(`/api/appointments/${appointmentId}/package-credits`)
  if (!res.ok) throw await failure(res, 'Could not load the client’s packages')
  return (await res.json()).credits
}

export async function cancelAppointment(id: string, reason?: string | null): Promise<Appointment> {
  const res = await send('POST', `/api/appointments/${id}/cancel`, { reason: reason || null })
  if (!res.ok) throw await failure(res, 'Could not cancel the appointment')
  return res.json()
}

export async function markNoShow(id: string): Promise<Appointment> {
  const res = await send('POST', `/api/appointments/${id}/no-show`, {})
  if (!res.ok) throw await failure(res, 'Could not mark this a no-show')
  return res.json()
}

/**
 * `staff_id` null is "any available": the server assigns the first free person by column
 * order. `starts_at` is a slot's own `starts_at` from `fetchAvailability`, never a time this
 * screen computed. Exactly one of `customer_id` and `customer`.
 *
 * A 409 with `code: 'slot_taken'` is the race lost: somebody booked it between the slots
 * being shown and this call. The screen refreshes the slots and says so.
 */
export type BookingDraft = {
  service_id: string
  staff_id: string | null
  starts_at: string
  customer_id?: string
  customer?: CustomerDraft
  notes?: string | null
} & Override

/**
 * The four advisory rules (tech-stack §22) a start may break: the ones a human may set aside,
 * on the record. A busy room or device, or a person at their concurrency limit, is not one of
 * these — those refusals come back as `not_offered` and nothing overrides them.
 */
export type OverrideRule = 'outside_shift' | 'time_off' | 'closure' | 'beyond_horizon'

/**
 * A booking or a move confirmed past the advisory rules. Own schedule: needs
 * `schedule.override_availability`. Somebody else's: `admin`, in Admin Mode, on top.
 */
export type Override = { override?: boolean; override_reason?: string | null }

/** The rules behind a 422 `override_available`, or null for any other refusal. */
export function overridableRules(error: unknown): OverrideRule[] | null {
  if (!(error instanceof ApiError) || error.code !== 'override_available') return null
  return (error.body as { rules?: OverrideRule[] }).rules ?? []
}

export async function bookAppointment(draft: BookingDraft): Promise<Appointment> {
  const res = await send('POST', '/api/appointments', draft)
  if (!res.ok) throw await failure(res, 'Could not book the appointment')
  return res.json()
}

// --- the calendar: one read for the grid, and the two drags -----------------------------------

/** A column on the grid: the roster entry without `sort_order` (the list is already in order). */
export type ScheduleColumn = {
  id: string
  display_name: string
  colour: string
  hex: string
  dark_hex: string
  /** How many appointments this person may run at once — the "×2" on the column header. */
  max_concurrent_appointments: number
}

/** One shift on one business-local date, as instants — converted server-side from the
 *  weekly rule, so the shading here and the slots the server offers are one picture. */
export type WorkingBlock = { staff_id: string; date: string; starts_at: string; ends_at: string }

export type ScheduleTimeOff = {
  id: string
  staff_id: string
  all_day: boolean
  reason: string | null
  starts_at: string
  ends_at: string
}

export type ScheduleClosure = { id: string; date: string; name: string }

export type Schedule = {
  timezone: string
  granularity_minutes: number
  staff: ScheduleColumn[]
  working_blocks: WorkingBlock[]
  time_off: ScheduleTimeOff[]
  closures: ScheduleClosure[]
  appointments: Appointment[]
}

/** `from`/`to` are business-local dates, `to` inclusive, at most 31 days apart; `staff_id`
 *  narrows every list to one column. Cancelled appointments are left out unless
 *  `include_cancelled` — the grid's "Show cancelled" toggle. */
export async function fetchSchedule(query: {
  from: string
  to: string
  staff_id?: string
  include_cancelled?: boolean
}): Promise<Schedule> {
  const params = new URLSearchParams({ from: query.from, to: query.to })
  if (query.staff_id) params.set('staff_id', query.staff_id)
  if (query.include_cancelled) params.set('include_cancelled', 'true')
  const res = await fetch(`/api/schedule?${params}`)
  if (!res.ok) throw await failure(res, 'Could not load the schedule')
  return res.json()
}

/**
 * A move (`starts_at`), a resize (`duration_minutes`), or both. The server re-runs the
 * engine with this appointment out of its own way and refuses with 422 `not_offered`, 422
 * `override_available` (with the rules a human may confirm past) or 409 `slot_taken` — the
 * same answers booking gives, treated the same way.
 */
export async function changeAppointment(
  id: string,
  change: { starts_at?: string; duration_minutes?: number } & Override,
): Promise<Appointment> {
  const res = await send('PATCH', `/api/appointments/${id}`, change)
  if (!res.ok) throw await failure(res, 'Could not move the appointment')
  return res.json()
}

// --- booking groups (Task 18) ----------------------------------------------------------------

/** One start of a chain, with the staff resolved per link — in the same order the services
 *  were asked in, so the picker can label each with the service it belongs to. */
export type GroupSlot = { starts_at: string; staff_ids: string[] }

export type GroupAvailability = {
  service_ids: string[]
  timezone: string
  granularity_minutes: number
  horizon_ends_on: string
  days: { date: string; slots: GroupSlot[] }[]
}

/**
 * Chain-valid starts for an ordered visit: every start of the first service from which
 * every following one is offered, back to back. `staffIds` is one per service, in order —
 * a person, or `null` for "any" — and defaults to "any" for every link when left out.
 */
export async function fetchGroupAvailability(query: {
  serviceIds: string[]
  from: string
  to: string
  staffIds?: (string | null)[]
}): Promise<GroupAvailability> {
  const params = new URLSearchParams({
    services: query.serviceIds.join(','),
    from: query.from,
    to: query.to,
  })
  if (query.staffIds) params.set('staff', query.staffIds.map((s) => s ?? 'any').join(','))
  const res = await fetch(`/api/availability/group?${params}`)
  if (!res.ok) throw await failure(res, 'Could not load the available times')
  return res.json()
}

export type GroupLinkDraft = { service_id: string; staff_id: string | null } & Override

export type GroupBookingDraft = {
  starts_at: string
  links: GroupLinkDraft[]
  customer_id?: string
  customer?: CustomerDraft
  notes?: string | null
}

export type Group = { booking_group_id: string; appointments: Appointment[] }

/** A refusal from `POST /appointments/group`: the same code a single booking would send for
 *  that link, plus which one. `overridableRules` and `stalePick`-style checks still work on
 *  the error directly; this reads the extra field. */
export function refusedLinkIndex(error: unknown): number | null {
  if (!(error instanceof ApiError)) return null
  const index = (error.body as { link_index?: unknown })?.link_index
  return typeof index === 'number' ? index : null
}

export async function bookGroup(draft: GroupBookingDraft): Promise<Group> {
  const res = await send('POST', '/api/appointments/group', draft)
  if (!res.ok) throw await failure(res, 'Could not book the visit')
  return res.json()
}

export async function cancelGroup(groupId: string, reason?: string | null): Promise<Group> {
  const res = await send('POST', `/api/appointments/group/${groupId}/cancel`, {
    reason: reason || null,
  })
  if (!res.ok) throw await failure(res, 'Could not cancel the visit')
  return res.json()
}

// --- public booking portal: `/book` and `/manage-booking` (Phase 6 Task 6, #10) --------------

/** One bookable service on the public portal — `CatalogService`'s shape pared to what an
 *  anonymous visitor may see: no `requirements`/`unbookable_reasons`, staff named rather than
 *  left as bare ids (a client picking a provider needs a name, same as the staff dialog). */
export type PublicService = {
  id: string
  name: string
  description: string | null
  duration_minutes: number
  price_cents: number
  staff: { id: string; display_name: string }[]
}

export type PublicServices = { online_booking_enabled: boolean; services: PublicService[] }

/** `online_booking_enabled: false` with an empty list is the whole portal being off, not a
 *  moment where no service happens to qualify — the page shows a different message for each. */
export async function fetchPublicServices(): Promise<PublicServices> {
  const res = await fetch('/api/public/booking/services', { credentials: 'omit', cache: 'no-store' })
  if (!res.ok) throw await failure(res, 'Could not load the services')
  return res.json()
}

/**
 * The public counterpart to `fetchAvailability` — same shape, `staff_id` omitted for "any
 * available". `credentials: 'omit'`: a staff browser previewing `/book` must not send its
 * session along (`fetchPublicForm`'s own reasoning). A 409 is a service the catalog no longer
 * says is bookable; a 404 is an unknown or not-online-bookable one.
 */
export async function fetchPublicAvailability(query: {
  service_id: string
  from: string
  to: string
  staff_id?: string
}): Promise<Availability> {
  const params = new URLSearchParams({ service_id: query.service_id, from: query.from, to: query.to })
  if (query.staff_id) params.set('staff_id', query.staff_id)
  const res = await fetch(`/api/public/booking/availability?${params}`, {
    credentials: 'omit',
    cache: 'no-store',
  })
  if (!res.ok) throw await failure(res, 'Could not load the available times')
  return res.json()
}

/** What the identity step sends, plus the honeypot (`website`): must arrive empty, or the
 *  server accepts the request with a 200 and books nothing — the detection is never revealed,
 *  so this client never branches on it either, same as the server's own docstring insists. */
export type PublicBookingDraft = {
  service_id: string
  staff_id: string | null
  starts_at: string
  customer: CustomerDraft
  website: string
}

/** What the confirmation screen shows. `management_link` is the only recovery path when no
 *  notification channel is configured — shown plainly, on this screen, never only emailed. */
export type PublicBooking = {
  appointment_id: string
  starts_at: string
  ends_at: string
  service_name: string
  staff_name: string
  management_link: string
}

/**
 * `POST /api/public/booking`, sent the same way the public forms endpoints are: no
 * credentials, and `referrerPolicy: 'origin'` so the server's Origin check (`main.py`) sees
 * this deployment's own origin rather than `Origin: null`, which the page's `no-referrer`
 * policy would otherwise produce for this one request (`fetchPublicForm`'s own note).
 */
export async function createPublicBooking(draft: PublicBookingDraft): Promise<PublicBooking> {
  const res = await fetch('/api/public/booking', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(draft),
    credentials: 'omit',
    referrerPolicy: 'origin',
    cache: 'no-store',
  })
  if (!res.ok) throw await failure(res, 'Could not book the appointment')
  return res.json()
}

/** What the booking-management sub-flow shows and acts on. `cancellable` gates both cancel
 *  and reschedule — one shared toggle, the server's own `ManageBookingOut` shape.
 *  `service_id`/`staff_id` are what the reschedule step hands `fetchPublicAvailability`. */
export type ManageBooking = {
  appointment_id: string
  status: string
  starts_at: string
  ends_at: string
  service_id: string
  service_name: string
  staff_id: string
  staff_name: string
  cancellable: boolean
}

async function manageRequest(path: string, body: unknown): Promise<ManageBooking> {
  const res = await fetch(`/api/public/booking/manage${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    credentials: 'omit',
    referrerPolicy: 'origin',
    cache: 'no-store',
  })
  // A dead link is a 404 `link_invalid`; the cutoff/toggle refusal is a 422
  // `online_change_not_allowed` — both carry `code`, which the page reads to word the two
  // differently rather than showing one generic failure for both.
  if (!res.ok) throw await failure(res, 'Could not open this booking')
  return res.json()
}

/** `token` is read from the URL fragment (`/manage-booking/#<token>`), never a path segment —
 *  the same reason the form-link lookup keeps it out of the path (`fetchPublicForm`). */
export const fetchManageBooking = (token: string) => manageRequest('', { token })
export const cancelManageBooking = (token: string) => manageRequest('/cancel', { token })
export const rescheduleManageBooking = (token: string, starts_at: string) =>
  manageRequest('/reschedule', { token, starts_at })

// --- walk-in queue ("take a number", #12) --------------------------------------------------

export type QueueEntryStatus = 'waiting' | 'in_service' | 'done' | 'abandoned'

/** `QueueEntryOut`'s shape (`scheduling/queue.py`). `estimated_wait_minutes` and
 *  `compliance_gaps` are only ever populated by `fetchQueueEntries` (the list read) — every
 *  other queue action's own single-entry response carries `null` for both, the server's own
 *  "not computed here" convention (Tasks 6/7's docstrings). */
export type QueueEntry = {
  id: string
  status: QueueEntryStatus
  arrived_at: string
  requested_service: { id: string; name: string }
  preferred_staff: { id: string; display_name: string } | null
  customer: { id: string; first_name: string; last_name: string; phone: string | null } | null
  bare_name: string | null
  bare_phone: string | null
  appointment_id: string | null
  /** An **estimate**, never a promise (Task 6's own labelling rule) — null for a non-waiting
   *  entry. */
  estimated_wait_minutes: number | null
  /** Only ever guaranteed complete for an essential template that applies to every client
   *  (`applies_to_all`) — a template scoped to the requested service can't yet "apply" to a
   *  walk-in with no confirmed appointment (Task 7's own documented gap). `null` outside the
   *  list read. */
  compliance_gaps: ComplianceEntry[] | null
}

export type QueueList = { entries: QueueEntry[] }

/**
 * `null` means `enable_walk_in_queue` is off — the server's whole-surface 404 (#12's own
 * acceptance criterion, "no queue surface exists anywhere in the product" while disabled),
 * never an error. `lib/nav.ts`'s gate and the queue screen itself both read this same shape,
 * off the same query key, so flipping the toggle needs no second endpoint to notice.
 */
export async function fetchQueueEntries(includeAbandoned = false): Promise<QueueList | null> {
  const qs = includeAbandoned ? '?include_abandoned=true' : ''
  const res = await fetch(`/api/queue-entries${qs}`, { cache: 'no-store' })
  if (res.status === 404) return null
  if (!res.ok) throw await failure(res, 'Could not load the queue')
  return res.json()
}

/** Exactly one of `customer_id`/`bare_name`, mirroring the server's own CHECK — "a walk-in
 *  may never become a full customer record" (CLAUDE.md). */
export type AddQueueEntryDraft = {
  customer_id?: string
  bare_name?: string
  bare_phone?: string | null
  requested_service_id: string
  preferred_staff_id?: string | null
}

export async function addQueueEntry(draft: AddQueueEntryDraft): Promise<QueueEntry> {
  const res = await send('POST', '/api/queue-entries', draft)
  if (!res.ok) throw await failure(res, 'Could not add this walk-in to the queue')
  return res.json()
}

export async function abandonQueueEntry(id: string): Promise<QueueEntry> {
  const res = await send('POST', `/api/queue-entries/${encodeURIComponent(id)}/abandon`, {})
  if (!res.ok) throw await failure(res, 'Could not update this queue entry')
  return res.json()
}

/** What `POST .../start` hands back: the queue entry (now `in_service`) and the ordinary
 *  `Appointment` it became — trimmed to what the queue screen's own success toast needs,
 *  not the calendar's full `Appointment` shape. */
export type QueueStartResult = {
  queue_entry: QueueEntry
  appointment: { id: string; staff: { display_name: string }; service: { name: string } }
}

export async function startQueueEntry(id: string): Promise<QueueStartResult> {
  const res = await send('POST', `/api/queue-entries/${encodeURIComponent(id)}/start`, {})
  if (!res.ok) throw await failure(res, 'Could not start this walk-in')
  return res.json()
}

// --- billing: tax components and their effective-dated rates (#57) ----------------------

export type TaxRate = {
  id: string
  rate_bp: number
  effective_from: string
  /** Null: still in effect. Never edited once closed — a new rate always adds a row. */
  effective_to: string | null
}

/** Whether a catalog price is entered before tax (tax added on top) or including it. */
export type TaxConvention = 'exclusive' | 'inclusive'

export type TaxComponent = {
  id: string
  code: string
  name: string
  /** Null: federal, applies whatever the business's own province is (e.g. GST). */
  province: string | null
  active: boolean
  rates: TaxRate[]
  /** The rate in effect today, in the business's own timezone. Null if the earliest rate is
   *  still in the future. */
  current_rate_bp: number | null
  /** A hint only: whether this business's own province would pick this component up
   *  (#57 acceptance criterion 1). Which components actually apply to one catalog item is a
   *  later ticket's job. */
  applicable_to_business: boolean
}

export type TaxComponentDraft = {
  code: string
  name: string
  province: string | null
  rate_bp: number
  effective_from: string
}

export async function fetchTaxComponents(): Promise<TaxComponent[]> {
  const res = await fetch('/api/admin/billing/tax-components')
  if (!res.ok) throw await failure(res, 'Could not load the tax components')
  return (await res.json()).tax_components
}

export async function createTaxComponent(draft: TaxComponentDraft): Promise<TaxComponent> {
  const res = await send('POST', '/api/admin/billing/tax-components', draft)
  if (!res.ok) throw await failure(res, 'Could not create the tax component')
  return res.json()
}

export async function updateTaxComponent(
  id: string,
  patch: Partial<Pick<TaxComponent, 'name' | 'province' | 'active'>>,
): Promise<TaxComponent> {
  const res = await send('PATCH', `/api/admin/billing/tax-components/${id}`, patch)
  if (!res.ok) throw await failure(res, 'Could not save the tax component')
  return res.json()
}

export async function addTaxComponentRate(
  id: string,
  rate: { rate_bp: number; effective_from: string },
): Promise<TaxComponent> {
  const res = await send('POST', `/api/admin/billing/tax-components/${id}/rates`, rate)
  if (!res.ok) throw await failure(res, 'Could not add the new rate')
  return res.json()
}

// --- bill review: draft bill + discounts + tax, together (#63) --------------------------

export type BillRef = { id: string; name: string }

export type LineTax = {
  pretax_cents: number
  /** Keyed by `TaxComponent.code` (e.g. `"GST"`) — every applicable component appears, `0`
   *  for one with no rate covering today, never omitted (`billing/tax.py`'s own rule). */
  component_cents: Record<string, number>
  tax_cents: number
  total_cents: number
}

export type BillLine = {
  id: string
  appointment_id: string
  service: BillRef
  staff: BillRef
  price_cents: number
  /** Non-zero when a package credit paid for this session (#72) — its frozen value. */
  prepaid_cents: number
  applied_discount_ids: string[]
  discounted_cents: number
  tax: LineTax
  line_total_cents: number
}

export type DiscountChoice = {
  id: string
  name: string
  kind: 'percentage' | 'fixed'
  percentage_bp: number | null
  amount_cents: number | null
  stackable: boolean
  /** Whether this discount is part of the combination currently applied to the bill. */
  applied: boolean
}

export type Bill = {
  id: string
  status: 'draft' | 'issued'
  customer: BillRef
  booking_group_id: string | null
  created_at: string
  updated_at: string
  lines: BillLine[]
  /** Every enabled discount eligible for at least one line — the picker's own choices, not
   *  only the ones currently applied. */
  eligible_discounts: DiscountChoice[]
  subtotal_cents: number
  discount_total_cents: number
  tax_totals_by_component: Record<string, number>
  tax_total_cents: number
  grand_total_cents: number
  /** An admin/owner-authorized exception (#64: an approved staff request, or a direct inline
   *  admin edit) — reported alongside `grand_total_cents` rather than replacing it. */
  override_total_cents: number | null
  override_reason: string | null
  bill_override_requests_enabled: boolean
  inline_admin_bill_edit_enabled: boolean
}

export type BillSummary = {
  id: string
  customer: BillRef
  booking_group_id: string | null
  created_at: string
  line_count: number
  subtotal_cents: number
}

export async function fetchDraftBills(): Promise<BillSummary[]> {
  const res = await fetch('/api/bills', { cache: 'no-store' })
  if (!res.ok) throw await failure(res, 'Could not load the draft bills')
  return (await res.json()).bills
}

export async function fetchBill(id: string): Promise<Bill> {
  const res = await fetch(`/api/bills/${encodeURIComponent(id)}`, { cache: 'no-store' })
  if (!res.ok) throw await failure(res, 'Could not load this bill')
  return res.json()
}

/**
 * Replaces the whole applied combination in one call — reapplying with a changed selection
 * is how "add or remove a discount" both work, the same "recomputed live" screen either way.
 * A 422 means the combination was refused (not stackable together, or exceeds the eligible
 * charge); `ApiError.message` carries the server's own specific reason verbatim.
 */
export async function applyBillDiscounts(id: string, discountIds: string[]): Promise<Bill> {
  const res = await send('PUT', `/api/bills/${encodeURIComponent(id)}/discounts`, {
    discount_ids: discountIds,
  })
  if (!res.ok) throw await failure(res, 'Could not apply these discounts')
  return res.json()
}

// --- bill review authority (#64): staff-request review + inline admin edit ----------------

export type OverrideRequest = {
  id: string
  bill_id: string
  kind: 'discount' | 'price_override'
  reason: string
  requested_total_cents: number
  requested_by_email: string
  requested_at: string
  bill_revision_as_of: string
  status: 'pending' | 'approved' | 'rejected'
  decided_by_email: string | null
  decided_at: string | null
  decision_note: string | null
  decided_total_cents: number | null
}

export async function fetchOverrideRequests(billId: string): Promise<OverrideRequest[]> {
  const res = await fetch(`/api/bills/${encodeURIComponent(billId)}/override-requests`, {
    cache: 'no-store',
  })
  if (!res.ok) throw await failure(res, 'Could not load the override requests')
  return (await res.json()).requests
}

export async function requestBillOverride(
  billId: string,
  body: { kind: 'discount' | 'price_override'; requested_total_cents: number; reason: string },
): Promise<OverrideRequest> {
  const res = await send('POST', `/api/bills/${encodeURIComponent(billId)}/override-requests`, body)
  if (!res.ok) throw await failure(res, 'Could not submit this request')
  return res.json()
}

/** Approve as-is (omit `decided_total_cents`), revise (include it), or reject. A 409 means the
 *  bill changed since the request was made — the stale-approval guard — and the caller should
 *  tell staff to resubmit rather than retry the same decision. */
export async function decideOverrideRequest(
  billId: string,
  requestId: string,
  body: { decision: 'approved' | 'rejected'; decided_total_cents?: number; note?: string },
): Promise<OverrideRequest> {
  const res = await send(
    'POST',
    `/api/bills/${encodeURIComponent(billId)}/override-requests/${encodeURIComponent(requestId)}/decision`,
    body,
  )
  if (!res.ok) throw await failure(res, 'Could not record this decision')
  return res.json()
}

export type InlineAdminStatus = {
  active: boolean
  admin_email: string | null
  hard_limit_at: string | null
}

export async function fetchInlineAdminStatus(billId: string): Promise<InlineAdminStatus> {
  const res = await fetch(`/api/bills/${encodeURIComponent(billId)}/inline-admin`, {
    cache: 'no-store',
  })
  if (!res.ok) throw await failure(res, 'Could not check the inline admin status')
  return res.json()
}

/** The admin/owner's own credentials, entered on the staff screen. `totp` is only sent when
 *  the account is enrolled and the server asks for one (`mfa_required`). */
export async function authenticateInlineAdmin(
  billId: string,
  body: { email: string; password: string; totp?: string },
): Promise<InlineAdminStatus> {
  const res = await send(
    'POST',
    `/api/bills/${encodeURIComponent(billId)}/inline-admin/authenticate`,
    body,
  )
  if (!res.ok) throw await failure(res, 'Could not authenticate')
  return res.json()
}

/** Ends inline authority early — called when staff navigate away from the bill while it is
 *  still active, so it never outlives the visit to the screen it was granted on. */
export async function releaseInlineAdmin(billId: string): Promise<void> {
  const res = await send('POST', `/api/bills/${encodeURIComponent(billId)}/inline-admin/release`)
  if (!res.ok) throw await failure(res, 'Could not release inline admin authority')
}

/** The supervised edit itself. One save consumes the authenticated window outright. */
export async function applyInlineAdminEdit(
  billId: string,
  body: { total_cents: number; reason: string },
): Promise<Bill> {
  const res = await send(
    'PUT',
    `/api/bills/${encodeURIComponent(billId)}/inline-admin/override`,
    body,
  )
  if (!res.ok) throw await failure(res, 'Could not save this edit')
  return res.json()
}

// --- the Invoices lists: service and retail, filtered and paginated (#99) -----------------

/** `outstanding | paid | cancelled` — derived server-side from the same balance the invoice
 *  view itself reads (`billing/payments.py::invoice_list_status`), never computed here. */
export type InvoiceListStatus = 'outstanding' | 'paid' | 'cancelled'

/** The fields every row of either list carries, beyond its own `id`/`invoice_number`/
 *  `customer_id`/`status`/`issued_at` — the balance breakdown `billing/payments.py::Balance`
 *  computes, the same figures the invoice view (#102) will show in full. */
export type InvoiceListBalance = {
  outstanding_cents: number
  pending_insurer_cents: number
  client_outstanding_cents: number
  checkout_complete: boolean
  refunded_cents: number
  prepaid_cents: number
  held_credit_cents: number
}

export type InvoiceSummary = InvoiceListBalance & {
  id: string
  invoice_number: number
  customer_id: string
  /** Never PHI — joined in server-side, so the row never triggers an audited profile read. */
  customer_name: string
  /** The raw ledger status (`issued`/`cancelled`) — `list_status` is the badge to show. */
  status: 'issued' | 'cancelled'
  list_status: InvoiceListStatus
  grand_total_cents: number
  issued_at: string
}

export type RetailInvoiceSummary = InvoiceListBalance & {
  id: string
  invoice_number: number
  /** `null` for an anonymous walk-in sale — the row reads "Walk-in". */
  customer_id: string | null
  customer_name: string | null
  status: 'issued' | 'cancelled'
  list_status: InvoiceListStatus
  grand_total_cents: number
  issued_at: string
}

/** Query params both lists share; `from`/`to` are business-local dates (server default: the
 *  last 30 days). */
type InvoiceListQuery = {
  customer_id?: string
  from?: string
  to?: string
  status?: InvoiceListStatus
  page?: number
  page_size?: number
}

function invoiceListParams(query: InvoiceListQuery): URLSearchParams {
  const params = new URLSearchParams()
  if (query.customer_id) params.set('customer_id', query.customer_id)
  if (query.from) params.set('from', query.from)
  if (query.to) params.set('to', query.to)
  if (query.status) params.set('status', query.status)
  if (query.page) params.set('page', String(query.page))
  if (query.page_size) params.set('page_size', String(query.page_size))
  return params
}

export type InvoiceList = {
  invoices: InvoiceSummary[]
  total: number
  /** The date range actually applied — the server's 30-day default when none was sent. */
  from: string
  to: string
  timezone: string
}

/** Service invoices only — `billing/invoices.py::list_invoices`. `?customer_id=` is the same
 *  narrowing (and the same audited read, logged only when filtered) a client's own Invoices
 *  tab reuses this call for. */
export async function fetchInvoices(query: InvoiceListQuery = {}): Promise<InvoiceList> {
  const res = await fetch(`/api/invoices?${invoiceListParams(query)}`, { cache: 'no-store' })
  if (!res.ok) throw await failure(res, 'Could not load the invoices')
  return res.json()
}

export type RetailInvoiceList = {
  retail_invoices: RetailInvoiceSummary[]
  total: number
  from: string
  to: string
  timezone: string
}

/** Retail invoices only — `billing/retail_sales.py::list_retail_invoices`, kept as its own
 *  list and its own numbering series (never merged with the service list). */
export async function fetchRetailInvoices(query: InvoiceListQuery = {}): Promise<RetailInvoiceList> {
  const res = await fetch(`/api/retail-invoices?${invoiceListParams(query)}`, { cache: 'no-store' })
  if (!res.ok) throw await failure(res, 'Could not load the retail invoices')
  return res.json()
}

// --- one invoice (#102): lines, discounts, per-line tax, the override adjustment, the full
// balance breakdown and replacement lineage both ways -----------------------------------------

export type InvoiceLineDiscount = {
  discount_id: string
  discount_name: string
  discount_kind: 'percentage' | 'fixed'
  percentage_bp: number | null
  amount_cents: number | null
  resolved_amount_cents: number | null
}

export type InvoiceLineTax = {
  component_code: string
  rate_bp: number
  amount_cents: number
}

export type InvoiceLine = {
  id: string
  appointment_id: string
  service_id: string
  service_name: string
  staff_id: string
  staff_name: string
  price_cents: number
  discounted_cents: number
  pretax_cents: number
  tax_cents: number
  line_total_cents: number
  prepaid_cents: number
  tax_convention: 'inclusive' | 'exclusive'
  /** Non-zero only under a bill's manual override (spread across the lines at issue). */
  override_adjustment_cents: number
  discounts: InvoiceLineDiscount[]
  taxes: InvoiceLineTax[]
}

/** `GET /api/invoices/{id}` — a service invoice, frozen at issue (`billing/invoices.py::
 *  InvoiceOut`). The full balance breakdown (`InvoiceListBalance`) plus lineage both ways. */
export type Invoice = InvoiceListBalance & {
  id: string
  invoice_number: number
  service_bill_id: string | null
  package_purchase_id: string | null
  customer_id: string
  customer_name: string
  status: 'issued' | 'cancelled'
  computed_subtotal_cents: number
  computed_discount_total_cents: number
  computed_tax_total_cents: number
  computed_grand_total_cents: number
  tax_totals_by_component: Record<string, number>
  tax_rates_by_component: Record<string, number>
  override_applied_cents: number | null
  override_reason: string | null
  override_tax_convention: 'inclusive' | 'exclusive' | null
  grand_total_cents: number
  issued_at: string
  issued_by: string
  /** #68 lineage, both ways: what this invoice replaced, and what replaced it. */
  replaces_invoice_id: string | null
  replaced_by_invoice_id: string | null
  cancelled_at: string | null
  cancel_reason: string | null
  lines: InvoiceLine[]
}

export async function fetchInvoice(id: string): Promise<Invoice> {
  const res = await fetch(`/api/invoices/${encodeURIComponent(id)}`, { cache: 'no-store' })
  if (!res.ok) throw await failure(res, 'Could not load this invoice')
  return res.json()
}

/** #102's other slot: issuing a reviewed bill lands here — `POST /bills/{id}/issue`. */
export async function issueBill(billId: string): Promise<Invoice> {
  const res = await send('POST', `/api/bills/${encodeURIComponent(billId)}/issue`, {})
  if (!res.ok) throw await failure(res, 'Could not issue this invoice')
  return res.json()
}

export type RetailInvoiceLine = {
  id: string
  variant_id: string
  variant_name: string
  quantity: number
  unit_price_cents: number
  discount_cents: number
  tax_cents: number
  tax_convention: 'inclusive' | 'exclusive'
  line_total_cents: number
  discounts: InvoiceLineDiscount[]
  taxes: InvoiceLineTax[]
  staff_id: string
}

/** `GET /api/retail-invoices/{id}` — the retail counterpart of `Invoice`, same balance and
 *  lineage shape; `customer_id`/`customer_name` are both `null` for an anonymous walk-in
 *  sale, which the screen renders as "Walk-in". */
export type RetailInvoice = InvoiceListBalance & {
  id: string
  invoice_number: number
  retail_sale_id: string
  customer_id: string | null
  customer_name: string | null
  status: 'issued' | 'cancelled'
  subtotal_cents: number
  discount_total_cents: number
  tax_total_cents: number
  tax_totals_by_component: Record<string, number>
  grand_total_cents: number
  sold_by_staff_id: string
  payment_collector_staff_id: string | null
  issued_at: string
  issued_by: string
  lines: RetailInvoiceLine[]
  replaces_invoice_id: string | null
  replaced_by_invoice_id: string | null
  cancelled_at: string | null
  cancel_reason: string | null
}

export async function fetchRetailInvoice(id: string): Promise<RetailInvoice> {
  const res = await fetch(`/api/retail-invoices/${encodeURIComponent(id)}`, { cache: 'no-store' })
  if (!res.ok) throw await failure(res, 'Could not load this retail invoice')
  return res.json()
}

// --- payments, corrections, refunds and balance exceptions (#103) -----------------------------
// The same ledger for both invoice kinds (`billing/payments.py`) — `invoiceBase` picks the one
// URL prefix difference, and every call below reads identically for a service or a retail id.

export type PaymentPayerType = 'client' | 'insurer'
export type PaymentMethod = 'cash' | 'e_transfer' | 'card' | 'insurer'
export type PaymentStatus = 'pending' | 'received'

export type PaymentEntry = {
  id: string
  invoice_id: string | null
  retail_invoice_id: string | null
  payer_type: PaymentPayerType
  method: PaymentMethod
  status: PaymentStatus
  amount_cents: number
  reference: string | null
  collected_by: string
  recorded_at: string
  /** Set on the *new* entry a correction creates — the id of the entry it supersedes. */
  corrects_payment_id: string | null
  correction_reason: string | null
}

export type PaymentTransfer = {
  id: string
  from_invoice_id: string
  to_invoice_id: string
  received_cents: number
  received_insurer_cents: number
  pending_insurer_cents: number
  transferred_by: string
  transferred_at: string
}

export type RefundEntry = {
  id: string
  invoice_id: string | null
  retail_invoice_id: string | null
  amount_cents: number
  reason: string
  approved_by: string
  refunded_at: string
}

export type BalanceException = {
  id: string
  invoice_id: string | null
  retail_invoice_id: string | null
  authorized_by: string
  reason: string
  outstanding_cents_at_authorization: number
  authorized_at: string
}

export type RecordPaymentBody = {
  payer_type: PaymentPayerType
  method: PaymentMethod
  amount_cents: number
  status?: PaymentStatus
  reference?: string | null
}

/** Only the fields being changed; the rest carry over from the entry being corrected
 *  (`billing/payments.py::CorrectPaymentIn`). */
export type CorrectPaymentBody = {
  reason: string
  payer_type?: PaymentPayerType
  method?: PaymentMethod
  amount_cents?: number
  reference?: string | null
}

function invoiceBase(kind: 'service' | 'retail', invoiceId: string): string {
  const prefix = kind === 'retail' ? '/api/retail-invoices' : '/api/invoices'
  return `${prefix}/${encodeURIComponent(invoiceId)}`
}

export async function fetchPayments(
  kind: 'service' | 'retail',
  invoiceId: string,
): Promise<{ payments: PaymentEntry[]; transfers: PaymentTransfer[] }> {
  const res = await fetch(`${invoiceBase(kind, invoiceId)}/payments`, { cache: 'no-store' })
  if (!res.ok) throw await failure(res, 'Could not load payments')
  return res.json()
}

export async function recordPayment(
  kind: 'service' | 'retail',
  invoiceId: string,
  body: RecordPaymentBody,
): Promise<PaymentEntry> {
  const res = await send('POST', `${invoiceBase(kind, invoiceId)}/payments`, body)
  if (!res.ok) throw await failure(res, 'Could not record this payment')
  return res.json()
}

export async function correctPayment(
  kind: 'service' | 'retail',
  invoiceId: string,
  paymentId: string,
  body: CorrectPaymentBody,
): Promise<PaymentEntry> {
  const res = await send(
    'POST',
    `${invoiceBase(kind, invoiceId)}/payments/${encodeURIComponent(paymentId)}/corrections`,
    body,
  )
  if (!res.ok) throw await failure(res, 'Could not correct this payment')
  return res.json()
}

export async function fetchRefunds(
  kind: 'service' | 'retail',
  invoiceId: string,
): Promise<{ refunds: RefundEntry[] }> {
  const res = await fetch(`${invoiceBase(kind, invoiceId)}/refunds`, { cache: 'no-store' })
  if (!res.ok) throw await failure(res, 'Could not load refunds')
  return res.json()
}

/** `billing.manage`, Admin Mode — capped server-side at money received. */
export async function recordRefund(
  kind: 'service' | 'retail',
  invoiceId: string,
  body: { amount_cents: number; reason: string },
): Promise<RefundEntry> {
  const res = await send('POST', `${invoiceBase(kind, invoiceId)}/refunds`, body)
  if (!res.ok) throw await failure(res, 'Could not record this refund')
  return res.json()
}

export async function fetchBalanceExceptions(
  kind: 'service' | 'retail',
  invoiceId: string,
): Promise<{ exceptions: BalanceException[] }> {
  const res = await fetch(`${invoiceBase(kind, invoiceId)}/balance-exceptions`, { cache: 'no-store' })
  if (!res.ok) throw await failure(res, 'Could not load balance exceptions')
  return res.json()
}

/** `billing.manage`, Admin Mode. */
export async function recordBalanceException(
  kind: 'service' | 'retail',
  invoiceId: string,
  body: { reason: string },
): Promise<BalanceException> {
  const res = await send('POST', `${invoiceBase(kind, invoiceId)}/balance-exceptions`, body)
  if (!res.ok) throw await failure(res, 'Could not record this balance exception')
  return res.json()
}

// --- CTI: phone lookup, demo mode, the simulated call (Phase 14, #16) -----------------------

/** A previous provider (`scheduling/cti.py`'s own shape): the most recent visit with each
 *  distinct staff member this client has completed a service with, newest first. */
export type PreviousProvider = {
  staff_id: string
  display_name: string
  colour: string
  service_name: string
  last_visit_at: string
}

/** One resolved match: the same non-PHI summary the Clients list and the booking dialog's
 *  search already send (`CustomerRecord`), so it can seed a quick-book the same way a picked
 *  search result already does. */
export type PhoneLookupMatch = {
  customer: CustomerRecord
  previous_providers: PreviousProvider[]
}

/** Three shapes off one endpoint: nobody matches, several people share this prefix (treated
 *  like search — `candidates` only), or exactly one does (treated like a profile open —
 *  `match` only). Never more than one of `candidates`/`match` populated at once. */
export type PhoneLookupResult = {
  status: 'no_match' | 'candidates' | 'match'
  candidates: CustomerRecord[]
  match: PhoneLookupMatch | null
}

/** Digits, formatting, or a partial number (at least 4 digits) — the server normalises
 *  either side the same way (`customers/phone.py`). */
export async function lookupPhone(phone: string): Promise<PhoneLookupResult> {
  const res = await fetch(`/api/cti/lookup?${new URLSearchParams({ phone })}`, { cache: 'no-store' })
  if (!res.ok) throw await failure(res, 'Could not look up that number')
  return res.json()
}

/** Whether the settings panel's demo-mode toggle is on — readable by any signed-in account
 *  holding `customers.view`, no Admin Mode needed (the write path is what requires it). */
export async function fetchDemoMode(): Promise<{ enabled: boolean }> {
  const res = await fetch('/api/cti/demo-mode', { cache: 'no-store' })
  if (!res.ok) throw await failure(res, 'Could not read the demo mode setting')
  return res.json()
}

/** A fake incoming call — nothing is persisted server-side. 404s while demo mode is off. */
export type CallEvent = { call_id: string; phone: string; received_at: string }

export async function simulateCall(): Promise<CallEvent> {
  const res = await send('POST', '/api/cti/simulate-call', {})
  if (!res.ok) throw await failure(res, 'Could not simulate a call')
  return res.json()
}

// --- reports: commission and package liability (#95/#110) --------------------------------

/** One posting behind a staff member's totals (`billing/commission_report.py::CommissionRowOut`
 *  verbatim) — a report row, never edited here. */
export type CommissionRow = {
  id: string
  posted_at: string
  kind: string
  source: 'service' | 'retail'
  staff_id: string
  staff_name: string
  invoice_id: string
  invoice_number: number
  invoice_status: string
  service_id: string | null
  variant_id: string | null
  commission_rate_bp: number
  basis_cents: number
  amount_cents: number
  payment_status: 'received' | 'partial' | 'pending' | 'voided'
  invoice_received_cents: number
  invoice_pending_cents: number
  commission_received_cents: number
  commission_pending_cents: number
}

export type CommissionSourceTotals = {
  revenue_cents: number
  commission_cents: number
  commission_received_cents: number
  commission_pending_cents: number
}

export type CommissionReport = {
  rows: CommissionRow[]
  service: CommissionSourceTotals
  retail: CommissionSourceTotals
  total_earned_cents: number
  total_received_cents: number
  total_pending_cents: number
  payments_received_cents: number
  payments_pending_cents: number
  from: string
  to: string
  timezone: string
}

/** `commission.view`, Admin Mode. `from`/`to` default to the server's own 90-day window when
 *  omitted (`billing/commission_report.py::DEFAULT_DAYS`). */
export async function fetchCommissionReport(query: {
  from?: string
  to?: string
  staff_id?: string
}): Promise<CommissionReport> {
  const params = new URLSearchParams()
  if (query.from) params.set('from', query.from)
  if (query.to) params.set('to', query.to)
  if (query.staff_id) params.set('staff_id', query.staff_id)
  const res = await fetch(`/api/admin/reports/commission?${params}`, { cache: 'no-store' })
  if (!res.ok) throw await failure(res, 'Could not load the commission report')
  return res.json()
}

export async function requestCommissionExport(query: {
  from?: string
  to?: string
  staff_id?: string
}): Promise<ExportJob> {
  const res = await post('/api/admin/reports/commission/exports', {
    from: query.from,
    to: query.to,
    staff_id: query.staff_id,
  })
  if (!res.ok) throw await failure(res, 'Could not request the export')
  return res.json()
}

export async function fetchCommissionExportStatus(exportId: string): Promise<ExportJob> {
  const res = await fetch(`/api/admin/reports/commission/exports/${encodeURIComponent(exportId)}`)
  if (!res.ok) throw await failure(res, 'Could not check the export')
  return res.json()
}

export function downloadCommissionExport(exportId: string): Promise<void> {
  return downloadExportFile(`/api/admin/reports/commission/exports/${encodeURIComponent(exportId)}/csv`)
}

/** One credit line behind a client's unused package value
 *  (`billing/package_liability.py::LiabilityRowOut` verbatim). */
export type PackageLiabilityRow = {
  customer_id: string
  customer_name: string
  package_purchase_id: string
  package_name: string
  purchased_at: string
  expires_at: string | null
  service_id: string
  service_name: string
  credits_total: number
  credits_redeemed: number
  credits_remaining: number
  unused_value_cents: number
}

export type PackageLiabilityCustomerTotal = {
  customer_id: string
  customer_name: string
  credits_remaining: number
  unused_value_cents: number
}

export type PackageLiabilityReport = {
  rows: PackageLiabilityRow[]
  customers: PackageLiabilityCustomerTotal[]
  total_unused_value_cents: number
  as_of: string
  from: string | null
  to: string | null
  timezone: string
}

/** `billing.manage`, Admin Mode. Unfiltered by default — every client with credits left. */
export async function fetchPackageLiabilityReport(query: {
  customer_id?: string
}): Promise<PackageLiabilityReport> {
  const params = new URLSearchParams()
  if (query.customer_id) params.set('customer_id', query.customer_id)
  const res = await fetch(`/api/admin/reports/package-liability?${params}`, { cache: 'no-store' })
  if (!res.ok) throw await failure(res, 'Could not load the package-liability report')
  return res.json()
}

export async function requestPackageLiabilityExport(query: {
  customer_id?: string
}): Promise<ExportJob> {
  const res = await post('/api/admin/reports/package-liability/exports', {
    customer_id: query.customer_id,
  })
  if (!res.ok) throw await failure(res, 'Could not request the export')
  return res.json()
}

export async function fetchPackageLiabilityExportStatus(exportId: string): Promise<ExportJob> {
  const res = await fetch(
    `/api/admin/reports/package-liability/exports/${encodeURIComponent(exportId)}`,
  )
  if (!res.ok) throw await failure(res, 'Could not check the export')
  return res.json()
}

export function downloadPackageLiabilityExport(exportId: string): Promise<void> {
  return downloadExportFile(
    `/api/admin/reports/package-liability/exports/${encodeURIComponent(exportId)}/csv`,
  )
}
