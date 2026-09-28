import { CalendarDays, ListOrdered, Package, Settings, Users, type LucideIcon } from 'lucide-react'
import type { User } from '@/lib/api'

export type NavItem = {
  to: string
  label: string
  icon: LucideIcon
  /**
   * Hold any one of these to see the entry. Absent means everybody sees it — most of the
   * app is staff work, and a nav that hides by default would need a capability invented for
   * every screen before it could be linked.
   */
  anyOf?: string[]
  /**
   * Present only for an entry gated by a business setting on top of the capability — the
   * entry is hidden whenever the flag is false, even for an account holding `anyOf`. Queue's
   * `enable_walk_in_queue` (#12) is the only one today: "with the queue disabled, no queue
   * surface exists anywhere in the product" (Task 1's own acceptance criterion), applied here
   * to the nav now that a real screen exists to hide.
   */
  requiresFlag?: 'queueEnabled'
}

/**
 * The administrative capabilities. Settings is the RBAC panel, and behind it every request
 * is refused without one of these — so offering the link to somebody holding none of them
 * leads only to an error paragraph, which reads as the app being broken rather than as a
 * door that was never theirs.
 *
 * Hiding is a courtesy, not the enforcement: `Requires` on each route is what actually
 * refuses, and typing the URL still gets the honest refusal rather than the screen.
 */
const ADMINISTRATIVE = ['admin', 'roles.manage', 'users.manage', 'catalog.manage', 'forms.manage']

export const NAV: NavItem[] = [
  { to: '/schedule', label: 'Schedule', icon: CalendarDays },
  { to: '/clients', label: 'Clients', icon: Users },
  {
    to: '/queue',
    label: 'Queue',
    icon: ListOrdered,
    anyOf: ['queue.manage'],
    requiresFlag: 'queueEnabled',
  },
  { to: '/catalog', label: 'Catalog', icon: Package },
  { to: '/settings', label: 'Settings', icon: Settings, anyOf: ADMINISTRATIVE },
]

/**
 * The entries this account should be offered. `flags` carries the business-setting gates
 * (just `queueEnabled` today) — omitted entirely hides every `requiresFlag` entry, which is
 * the safe default while that answer is still loading.
 */
export function navFor(user: User | null, flags: { queueEnabled?: boolean } = {}): NavItem[] {
  return NAV.filter((item) => {
    if (item.anyOf && !item.anyOf.some((c) => user?.capabilities?.includes(c))) return false
    if (item.requiresFlag && !flags[item.requiresFlag]) return false
    return true
  })
}
