import type { ComponentType } from 'react'
import { useSearchParams } from 'react-router'
import { SettingsSidebar } from '@/components/settings-sidebar'
import { isTabValue, type TabValue } from '@/lib/settings-nav'
import { BrandingPanel } from '@/routes/settings-branding'
import { BusinessPanel } from '@/routes/settings-business'
import { ClosuresPanel } from '@/routes/settings-closures'
import { FormsPanel } from '@/routes/settings-forms'
import { NoteTemplatesPanel } from '@/routes/settings-notes'
import { NotificationsPanel } from '@/routes/settings-notifications'
import { PackagesPanel } from '@/routes/settings-packages'
import { ProductsPanel } from '@/routes/settings-products'
import { ResourcesPanel } from '@/routes/settings-resources'
import { ServicesPanel } from '@/routes/settings-services'
import { RolesPanel } from '@/routes/settings-roles'
import { SecurityPanel } from '@/routes/settings-security'
import { StaffPanel } from '@/routes/settings-staff'
import { TaxSettingsPanel } from '@/routes/settings-tax'

/** One panel per `?tab=` value — the sidebar (`settings-sidebar.tsx`) owns the grouping and
 *  labels; this is just where each value's content comes from. */
const PANELS = {
  business: BusinessPanel,
  branding: BrandingPanel,
  closures: ClosuresPanel,
  staff: StaffPanel,
  roles: RolesPanel,
  security: SecurityPanel,
  resources: ResourcesPanel,
  services: ServicesPanel,
  products: ProductsPanel,
  packages: PackagesPanel,
  tax: TaxSettingsPanel,
  forms: FormsPanel,
  notes: NoteTemplatesPanel,
  notifications: NotificationsPanel,
} satisfies Record<TabValue, ComponentType>

/**
 * Everything about the business itself (PRD §1, §7).
 *
 * A collapsible sidebar (`SettingsSidebar`) stands in for the line tabs this page used to
 * open on — fourteen of them stopped fitting a single row a while ago. Grouping them into
 * sections is the sidebar's job; this component only resolves `?tab=` to a panel and hands
 * the active value back and forth.
 *
 * Every panel needs Admin Mode. Nothing here hides itself when the window lapses; the panels
 * surface the server's refusal instead, because a screen that empties on expiry looks broken
 * rather than locked.
 *
 * **The open tab lives in `?tab=`** (#116), not only in local state: the onboarding checklist
 * links straight to the tab a step belongs to (`/settings?tab=tax`), and a reload of that link
 * has to land on the same tab rather than snapping back to Business.
 */
export function SettingsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const requested = searchParams.get('tab')
  const tab: TabValue = isTabValue(requested) ? requested : 'business'
  const Panel = PANELS[tab]

  return (
    <div className="flex flex-col gap-4 md:flex-row md:items-start md:gap-6">
      <SettingsSidebar
        active={tab}
        onSelect={(value) => setSearchParams({ tab: value }, { replace: true })}
      />
      <div data-slot="settings-panel" className="min-w-0 flex-1">
        <Panel />
      </div>
    </div>
  )
}
