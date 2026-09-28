/**
 * Query keys shared by more than one screen.
 *
 * Here rather than beside the component that happens to fetch first: the Roles panel and the
 * People panel both read and invalidate the role list, and two spellings of one key would
 * mean a role created on one never reaching the select on the other — a bug that looks like
 * a stale server rather than a typo. Keys used by a single screen stay in that screen.
 */
export const ROLES = ['roles'] as const
export const CAPABILITIES = ['capabilities'] as const
/** The roster behind Settings → Staff, with the account facts on each row. */
export const STAFF = ['staff'] as const
/** The curated colours. Served by the backend, so the calendar and the picker agree. */
export const STAFF_PALETTE = ['staff-palette'] as const
/** Spaces and equipment. One key, filtered by `kind` in the query itself — a space created
 *  on one tab must invalidate what the other tab has cached too, and a shared prefix does
 *  that in one call. */
export const RESOURCES = ['resources'] as const
/** One staff member's weekly matrix, and one staff member's absences. Both take the staff
 *  id as a second element, so a prefix invalidation clears every person the tab has seen. */
export const STAFF_HOURS = ['staff-hours'] as const
export const TIME_OFF = ['time-off'] as const
/** The days the business is shut, by year. */
export const CLOSURES = ['closures'] as const
export const MFA = ['mfa'] as const
export const SECURITY = ['security-policy'] as const
/** Settings → Notifications: sender, SMS, templates, reminder intervals (#11). */
export const NOTIFICATION_SETTINGS = ['notification-settings'] as const
/** The public branding document: read by the shell, invalidated by the Branding panel. */
export const BRANDING_DOCUMENT = ['branding'] as const
export const BUSINESS = ['business'] as const
export const BRANDING = ['business-branding'] as const
/** The service catalog. One key: the table and the create dialog both read it, and a
 *  service created in the dialog has to reach the table behind it. */
export const SERVICES = ['services'] as const
/** The retail catalog: products and their variants (M4 #56). */
export const PRODUCTS = ['products'] as const
/** The schedule's column roster (`/api/staff`), and the appointments on a day. The list key
 *  takes the date as a second element, so booking invalidates every day the tab has seen. */
export const ROSTER = ['roster'] as const
export const APPOINTMENTS = ['appointments'] as const
/** The catalog as the booking screen reads it — a different endpoint and shape from
 *  `SERVICES`, which is the administrator's editing view. */
export const CATALOG = ['catalog'] as const
export const AVAILABILITY = ['availability'] as const
/** The customer list, in both its shapes: the booking dialog's search takes the query string
 *  as a second element; the Clients page takes the query, then the page. One prefix, so a
 *  client created in the dialog reaches the list too. */
export const CUSTOMERS = ['customers'] as const
/** The grid's one read (`/api/schedule`); takes the range and the staff filter after it.
 *  Booking, moving and resizing invalidate the prefix. */
export const SCHEDULE = ['schedule'] as const
/** One client's profile, with their visit list; takes the client id after it. Scheduling
 *  changes invalidate the prefix, because the profile's visits (and the erasure dialog's
 *  upcoming-visit count) are read from it. */
export const CUSTOMER_PROFILE = ['customer-profile'] as const
/** Settings → Forms: the template list, and one template's version history (id after it). */
export const FORM_TEMPLATES = ['form-templates'] as const
export const FORM_VERSIONS = ['form-versions'] as const
/** The "Send form" dialog's choices (`forms.issue`), and one client's open links (id after). */
export const SENDABLE_FORMS = ['sendable-forms'] as const
export const FORM_LINKS = ['form-links'] as const
/** One client's completed forms (`forms.view`; metadata), id after; and one opened (logged). */
export const FORM_SUBMISSIONS = ['form-submissions'] as const
export const FORM_SUBMISSION = ['form-submission'] as const
/** One client's essential-forms compliance (id after): the profile banner. */
export const COMPLIANCE = ['compliance'] as const
/** The "Forms needed" dashboard (`forms.issue`): clients with an upcoming appointment and a
 *  non-compliant essential form. */
export const FORMS_NEEDED = ['forms-needed'] as const
export const NOTE_TEMPLATES = ['note-templates'] as const
export const SESSION_NOTES = ['session-notes'] as const
export const SESSION_NOTE = ['session-note'] as const
export const NOTE_APPOINTMENTS = ['note-appointments'] as const
/** The walk-in queue (`queue.manage`, #12): waiting/in-service entries, wait estimates and
 *  compliance gaps recomputed live on every read. `null` cached under this key means
 *  `enable_walk_in_queue` is off (the server's whole-surface 404) — `lib/nav.ts`'s gate and
 *  the queue screen itself both read that shape off the same query, one fetch either way. */
export const QUEUE = ['queue'] as const
