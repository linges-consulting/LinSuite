import { ShieldCheck } from 'lucide-react'
import { EmptyState } from '@/components/empty-state'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useCan } from '@/lib/capability-gate'
import { CommissionReportTab } from '@/routes/reports-commission'
import { PackageLiabilityReportTab } from '@/routes/reports-package-liability'

/**
 * Reports (#97/#95/#110/#114): Commission and Package liability, tables only, each with its
 * own CSV export. `lib/nav.ts`'s own `anyOf` is what hides the nav entry from an account
 * holding neither `commission.view` nor `billing.manage`, and `App.tsx`'s `RequireAdminMode`
 * is what keeps this whole route from rendering at all in Staff Mode — so by the time this
 * component runs, the only way to hold neither capability is a direct URL, which is a
 * permission gap rather than a mode one, and the empty state below says so without mentioning
 * a mode the visitor is already in.
 */
export function ReportsPage() {
  const canCommission = useCan('commission.view')
  const canLiability = useCan('billing.manage')
  const defaultTab = canCommission ? 'commission' : 'package-liability'

  if (!canCommission && !canLiability) {
    return (
      <EmptyState
        icon={ShieldCheck}
        title="No reports available"
        description="Your role doesn't hold the commission or billing capability that reports need."
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
