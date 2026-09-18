import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { RolesPanel } from '@/routes/settings-roles'
import { UsersPanel } from '@/routes/settings-users'

/**
 * The RBAC & User Management panel (PRD §7).
 *
 * Tabs rather than two nav entries: roles and accounts are one job done in two passes —
 * you define what a role may do, then you put people on it — and splitting them across the
 * sidebar would make the second half of that job something you go looking for.
 *
 * Both panels need Admin Mode. Nothing here hides itself when the window lapses; the panels
 * surface the server's refusal instead, because a screen that empties on expiry looks broken
 * rather than locked.
 */
export function SettingsPage() {
  return (
    <Tabs defaultValue="roles" className="gap-4">
      <TabsList>
        <TabsTrigger value="roles">Roles</TabsTrigger>
        <TabsTrigger value="users">People</TabsTrigger>
      </TabsList>
      <TabsContent value="roles">
        <RolesPanel />
      </TabsContent>
      <TabsContent value="users">
        <UsersPanel />
      </TabsContent>
    </Tabs>
  )
}
