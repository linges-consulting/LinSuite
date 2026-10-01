import {
  Armchair,
  Bell,
  Building2,
  CalendarOff,
  FileText,
  Layers,
  Lock,
  NotebookPen,
  Package,
  Palette,
  Percent,
  Scissors,
  UserCog,
  Users,
  type LucideIcon,
} from 'lucide-react'

/** Every panel Settings can open on. One list so `?tab=` has something to validate against
 *  (#116's onboarding checklist links here as `/settings?tab=<key>`) — an unrecognised or
 *  missing value falls back to Business rather than rendering nothing. */
export const TAB_VALUES = [
  'business',
  'branding',
  'closures',
  'staff',
  'roles',
  'security',
  'resources',
  'services',
  'products',
  'packages',
  'tax',
  'forms',
  'notes',
  'notifications',
] as const
export type TabValue = (typeof TAB_VALUES)[number]

export function isTabValue(value: string | null): value is TabValue {
  return (TAB_VALUES as readonly string[]).includes(value ?? '')
}

type SettingsItem = { value: TabValue; label: string; icon: LucideIcon }
type SettingsSection = { id: string; label: string; items: SettingsItem[] }

/** The Settings sidebar's grouping (`settings-sidebar.tsx`). Order is the order the rail
 *  renders in. */
export const SETTINGS_SECTIONS: SettingsSection[] = [
  {
    id: 'business',
    label: 'Business',
    items: [
      { value: 'business', label: 'Business profile', icon: Building2 },
      { value: 'branding', label: 'Branding', icon: Palette },
      { value: 'closures', label: 'Closures', icon: CalendarOff },
    ],
  },
  {
    id: 'people',
    label: 'People & access',
    items: [
      { value: 'staff', label: 'Staff', icon: Users },
      { value: 'roles', label: 'Roles', icon: UserCog },
      { value: 'security', label: 'Security', icon: Lock },
    ],
  },
  {
    id: 'catalog',
    label: 'Catalog',
    items: [
      { value: 'resources', label: 'Spaces & equipment', icon: Armchair },
      { value: 'services', label: 'Services', icon: Scissors },
      { value: 'products', label: 'Products', icon: Package },
      { value: 'packages', label: 'Packages', icon: Layers },
    ],
  },
  {
    id: 'billing',
    label: 'Billing',
    items: [{ value: 'tax', label: 'Tax', icon: Percent }],
  },
  {
    id: 'clients',
    label: 'Clients & records',
    items: [
      { value: 'forms', label: 'Forms', icon: FileText },
      { value: 'notes', label: 'Note templates', icon: NotebookPen },
    ],
  },
  {
    id: 'communication',
    label: 'Communication',
    items: [{ value: 'notifications', label: 'Notifications', icon: Bell }],
  },
]
