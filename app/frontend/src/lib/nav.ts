import { CalendarDays, Package, Settings, Users, type LucideIcon } from 'lucide-react'
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
  { to: '/catalog', label: 'Catalog', icon: Package },
  { to: '/settings', label: 'Settings', icon: Settings, anyOf: ADMINISTRATIVE },
]

/** The entries this account should be offered. */
export function navFor(user: User | null): NavItem[] {
  return NAV.filter(
    (item) => !item.anyOf || item.anyOf.some((c) => user?.capabilities?.includes(c)),
  )
}
