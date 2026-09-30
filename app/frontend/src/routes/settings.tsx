import { useSearchParams } from 'react-router'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
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

/** Every tab this page can open on. Kept as one list so `?tab=` has something to validate
 *  against — the onboarding checklist (#116) links here as `/settings?tab=<key>`, and an
 *  unrecognised or missing value falls back to Business rather than rendering nothing. */
const TAB_VALUES = [
  'business',
  'branding',
  'roles',
  'staff',
  'resources',
  'services',
  'products',
  'packages',
  'tax',
  'forms',
  'notes',
  'closures',
  'notifications',
  'security',
] as const
type TabValue = (typeof TAB_VALUES)[number]

function isTabValue(value: string | null): value is TabValue {
  return TAB_VALUES.includes(value as TabValue)
}

/**
 * Everything about the business itself (PRD §1, §7).
 *
 * Tabs rather than a nav entry each: these are the settings you visit, change one thing in,
 * and leave. Business comes first because it is the one a new instance has to fill in — the
 * wizard collects a name and a timezone and nothing else.
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

  return (
    <Tabs
      value={tab}
      onValueChange={(value) => setSearchParams({ tab: value }, { replace: true })}
      className="gap-4"
    >
      <TabsList className="h-auto flex-wrap justify-start">
        <TabsTrigger value="business">Business</TabsTrigger>
        <TabsTrigger value="branding">Branding</TabsTrigger>
        <TabsTrigger value="roles">Roles</TabsTrigger>
        <TabsTrigger value="staff">Staff</TabsTrigger>
        <TabsTrigger value="resources">Resources</TabsTrigger>
        <TabsTrigger value="services">Services</TabsTrigger>
        <TabsTrigger value="products">Products</TabsTrigger>
        <TabsTrigger value="packages">Packages</TabsTrigger>
        <TabsTrigger value="tax">Tax</TabsTrigger>
        <TabsTrigger value="forms">Forms</TabsTrigger>
        <TabsTrigger value="notes">Note templates</TabsTrigger>
        <TabsTrigger value="closures">Closures</TabsTrigger>
        <TabsTrigger value="notifications">Notifications</TabsTrigger>
        <TabsTrigger value="security">Security</TabsTrigger>
      </TabsList>
      <TabsContent value="business">
        <BusinessPanel />
      </TabsContent>
      <TabsContent value="branding">
        <BrandingPanel />
      </TabsContent>
      <TabsContent value="roles">
        <RolesPanel />
      </TabsContent>
      <TabsContent value="staff">
        <StaffPanel />
      </TabsContent>
      <TabsContent value="resources">
        <ResourcesPanel />
      </TabsContent>
      <TabsContent value="services">
        <ServicesPanel />
      </TabsContent>
      <TabsContent value="products">
        <ProductsPanel />
      </TabsContent>
      <TabsContent value="packages">
        <PackagesPanel />
      </TabsContent>
      <TabsContent value="tax">
        <TaxSettingsPanel />
      </TabsContent>
      <TabsContent value="forms">
        <FormsPanel />
      </TabsContent>
      <TabsContent value="notes">
        <NoteTemplatesPanel />
      </TabsContent>
      <TabsContent value="closures">
        <ClosuresPanel />
      </TabsContent>
      <TabsContent value="notifications">
        <NotificationsPanel />
      </TabsContent>
      <TabsContent value="security">
        <SecurityPanel />
      </TabsContent>
    </Tabs>
  )
}
