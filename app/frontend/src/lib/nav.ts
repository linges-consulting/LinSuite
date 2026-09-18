import { CalendarDays, Package, Settings, Users, type LucideIcon } from 'lucide-react'

export type NavItem = { to: string; label: string; icon: LucideIcon }

export const NAV: NavItem[] = [
  { to: '/schedule', label: 'Schedule', icon: CalendarDays },
  { to: '/clients', label: 'Clients', icon: Users },
  { to: '/catalog', label: 'Catalog', icon: Package },
  { to: '/settings', label: 'Settings', icon: Settings },
]
