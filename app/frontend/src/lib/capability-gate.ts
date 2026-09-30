import { useSession } from '@/lib/auth'

/**
 * The capability/mode gate (#97, spec #95's "Mode and capability rule"): the one thing every
 * Admin-only action in M6 renders through. An action is offered only when the account holds
 * the capability *and*, for a capability the registry marks administrative, the session is
 * currently in Admin Mode — never a disabled control with an explanation (#95 IA decision:
 * "absent, never disabled").
 *
 * **Where this list comes from.** The registry itself (`auth/capabilities.py`'s
 * `requires_admin_mode`) is the source of truth, but the one endpoint that serves it as data
 * — `GET /admin/capabilities` — requires `roles.manage`, which is itself administrative. Most
 * accounts that need this gate (front-desk Sell, a client's Packages tab) hold none of the
 * administrative capabilities, so they could never fetch the registry to ask "does this one
 * need Admin Mode?" — the fetch that would answer the question is behind the same wall the
 * question is about. A fixed list is the simplest correct source, the same posture `lib/nav.ts`
 * already takes with its own `ADMINISTRATIVE` array. Keep this in sync with `capabilities.py`
 * by hand; nothing enforces the two agreeing.
 *
 * Exported so `lib/nav.ts` can gate a nav entry by the same rule instead of guessing at its
 * own copy of "which of these capabilities are administrative."
 */
export const ADMIN_MODE_CAPABILITIES: ReadonlySet<string> = new Set([
  'admin',
  'roles.manage',
  'users.manage',
  'catalog.manage',
  'audit.view',
  'forms.manage',
  'notes.manage',
  'customers.erase',
  'billing.manage',
  'commission.view',
  'inventory.receive',
  'inventory.adjust',
])

/**
 * True only when the signed-in account holds `capability` and, if that capability requires
 * Admin Mode, the session is in Admin Mode right now. False in every other case, including
 * while the session is still loading — the same "hidden while unknown" default `navFor`
 * documents for its own flags.
 */
export function useCan(capability: string): boolean {
  const { user } = useSession()
  if (!user?.capabilities?.includes(capability)) return false
  return !ADMIN_MODE_CAPABILITIES.has(capability) || user.mode === 'admin'
}
