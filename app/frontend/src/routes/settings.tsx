import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { BrandingPanel } from '@/routes/settings-branding'
import { BusinessPanel } from '@/routes/settings-business'
import { ClosuresPanel } from '@/routes/settings-closures'
import { ResourcesPanel } from '@/routes/settings-resources'
import { RolesPanel } from '@/routes/settings-roles'
import { SecurityPanel } from '@/routes/settings-security'
import { StaffPanel } from '@/routes/settings-staff'

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
 */
export function SettingsPage() {
  return (
    <Tabs defaultValue="business" className="gap-4">
      <TabsList>
        <TabsTrigger value="business">Business</TabsTrigger>
        <TabsTrigger value="branding">Branding</TabsTrigger>
        <TabsTrigger value="roles">Roles</TabsTrigger>
        <TabsTrigger value="staff">Staff</TabsTrigger>
        <TabsTrigger value="resources">Resources</TabsTrigger>
        <TabsTrigger value="closures">Closures</TabsTrigger>
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
      <TabsContent value="closures">
        <ClosuresPanel />
      </TabsContent>
      <TabsContent value="security">
        <SecurityPanel />
      </TabsContent>
    </Tabs>
  )
}
