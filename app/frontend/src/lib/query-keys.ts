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
export const ACCOUNTS = ['accounts'] as const
export const MFA = ['mfa'] as const
export const SECURITY = ['security-policy'] as const
