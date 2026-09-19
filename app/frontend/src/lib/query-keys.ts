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
export const MFA = ['mfa'] as const
export const SECURITY = ['security-policy'] as const
/** The public branding document: read by the shell, invalidated by the Branding panel. */
export const BRANDING_DOCUMENT = ['branding'] as const
export const BUSINESS = ['business'] as const
export const BRANDING = ['business-branding'] as const
