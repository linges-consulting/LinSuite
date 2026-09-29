import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { CommissionReportTab } from '@/routes/reports-commission'
import { PackageLiabilityReportTab } from '@/routes/reports-package-liability'

/**
 * Reports (#97/#95): Commission and Package liability, tables only, each with its own CSV
 * export once it lands. `lib/nav.ts`'s own `anyOf` is what hides the nav entry from an
 * account holding neither `commission.view` nor `billing.manage`; both reports themselves
 * also require Admin Mode, enforced by the server once each has real data to refuse.
 */
export function ReportsPage() {
  return (
    <Tabs defaultValue="commission" className="gap-4">
      <TabsList>
        <TabsTrigger value="commission">Commission</TabsTrigger>
        <TabsTrigger value="package-liability">Package liability</TabsTrigger>
      </TabsList>
      <TabsContent value="commission">
        <CommissionReportTab />
      </TabsContent>
      <TabsContent value="package-liability">
        <PackageLiabilityReportTab />
      </TabsContent>
    </Tabs>
  )
}
