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
/** Package definitions behind Settings → Packages (#101). */
export const PACKAGE_DEFINITIONS = ['package-definitions'] as const
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
/** Settings → Billing → Tax: components and their effective-dated rates (#57). */
export const TAX_COMPONENTS = ['tax-components'] as const
/** Bill review (#63): the draft-bill list, and one bill (id after) with its lines, eligible
 *  discounts and tax — recomputed by the server on every read/apply, never cached stale
 *  across a discount toggle. */
export const DRAFT_BILLS = ['draft-bills'] as const
export const BILL = ['bill'] as const
/** Bill review authority (#64): one bill's override requests, and its inline-admin window
 *  status — both take the bill id after them. */
export const OVERRIDE_REQUESTS = ['override-requests'] as const
export const INLINE_ADMIN = ['inline-admin'] as const
/** CTI (Phase 14, #16): a phone lookup, keyed by the digits searched — the phone-lookup
 *  page and the screen-pop panel share the same cache entry for the same number. Whether
 *  the demo-mode toggle is on, read by the phone-lookup page to decide whether to render
 *  the simulate control at all. */
export const PHONE_LOOKUP = ['phone-lookup'] as const
export const DEMO_MODE = ['cti-demo-mode'] as const
/** Reports (#95/#110): the commission report, filtered by date range and staff, and the
 *  package-liability report, filtered by client — each recomputed live on every read, never
 *  cached across a filter change beyond what the query params already key on. */
export const COMMISSION_REPORT = ['commission-report'] as const
export const PACKAGE_LIABILITY_REPORT = ['package-liability-report'] as const
/** The Invoices tab (#99): service and retail issued invoices, each its own key since they
 *  are never merged into one list — filters (date/status/client) and the page follow after. */
export const INVOICES = ['invoices'] as const
export const RETAIL_INVOICES = ['retail-invoices'] as const
/** One invoice (#102): the invoice view's own read, id after — a different key from the list
 *  above (`INVOICES`/`RETAIL_INVOICES`) since issuing a bill or cancelling an invoice
 *  invalidates one row's detail without refetching every page of the list. */
export const INVOICE = ['invoice'] as const
export const RETAIL_INVOICE = ['retail-invoice'] as const
/** The payments panel (#103): one invoice's payment ledger, refunds and balance exceptions —
 *  each takes `kind` then the invoice id after it, so a service and a retail invoice sharing
 *  no id space never collide in the cache. */
export const INVOICE_PAYMENTS = ['invoice-payments'] as const
export const INVOICE_REFUNDS = ['invoice-refunds'] as const
export const INVOICE_BALANCE_EXCEPTIONS = ['invoice-balance-exceptions'] as const
/** Sell (#106): the product catalog Sell's search reads (`/api/catalog/products`) — a
 *  different key from `CATALOG`, which is the booking screen's own services read. Open
 *  drafts (newest first, capped), and one draft sale's own read/write, id after. */
export const PRODUCT_CATALOG = ['product-catalog'] as const
export const OPEN_RETAIL_SALES = ['open-retail-sales'] as const
export const RETAIL_SALE = ['retail-sale'] as const
/** The client Packages tab (#108): one client's package purchases, id after — a different key
 *  from `PACKAGE_DEFINITIONS` (Settings → Packages, admin) and from the staff-facing sellable
 *  list below, since purchasing invalidates only this client's own history. */
export const CLIENT_PACKAGE_PURCHASES = ['client-package-purchases'] as const
/** The *Sell package* dialog's own read (#108): active packages, `billing.view`, Staff Mode. */
export const SELLABLE_PACKAGES = ['sellable-packages'] as const
/** One package purchase's frozen shape (#109/#102 gap fix): `GET /api/packages/purchases/{id}`,
 *  id after — the refund dialog's own pre-selection read and the package invoice view's own
 *  read, kept separate from `CLIENT_PACKAGE_PURCHASES` (a client's whole list). */
export const PACKAGE_PURCHASE = ['package-purchase'] as const
/** The Transfer dialog's own warning (#111): the current holder's upcoming appointments for
 *  a purchase's covered services, id after. */
export const PACKAGE_TRANSFER_UPCOMING = ['package-transfer-upcoming'] as const
