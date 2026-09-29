import { ShieldCheck } from 'lucide-react'
import { EmptyState } from '@/components/empty-state'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useCan } from '@/lib/capability-gate'
import { CommissionReportTab } from '@/routes/reports-commission'
import { PackageLiabilityReportTab } from '@/routes/reports-package-liability'

/**
 * Reports (#97/#95/#110): Commission and Package liability, tables only, each with its own
 * CSV export. `lib/nav.ts`'s own `anyOf` is what hides the nav entry from an account holding
 * neither `commission.view` nor `billing.manage`; here, each tab is offered only when its own
 * capability passes `useCan` — both are administrative, so absent in Staff Mode too (spec #95
 * "Mode and capability rule": absent, never disabled).
 */
export function ReportsPage() {
  const canCommission = useCan('commission.view')
  const canLiability = useCan('billing.manage')
  const defaultTab = canCommission ? 'commission' : 'package-liability'

  if (!canCommission && !canLiability) {
    return (
      <EmptyState
        icon={ShieldCheck}
        title="Switch to Admin Mode"
        description="Reports need Admin Mode and the capability to view them."
      />
    )
  }

  return (
    <Tabs defaultValue={defaultTab} className="gap-4">
      <TabsList>
        {canCommission && <TabsTrigger value="commission">Commission</TabsTrigger>}
        {canLiability && <TabsTrigger value="package-liability">Package liability</TabsTrigger>}
      </TabsList>
      {canCommission && (
        <TabsContent value="commission">
          <CommissionReportTab />
        </TabsContent>
      )}
      {canLiability && (
        <TabsContent value="package-liability">
          <PackageLiabilityReportTab />
        </TabsContent>
      )}
    </Tabs>
  )
}
