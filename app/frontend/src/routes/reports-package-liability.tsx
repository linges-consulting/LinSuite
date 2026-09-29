import { BarChart3 } from 'lucide-react'
import { EmptyState } from '@/components/empty-state'

/**
 * Package liability (spec #95 user story 68): a client filter, outstanding package credits
 * and their value, CSV export. `billing.manage`, Admin Mode. This file is that later ticket's
 * slot — empty until it lands.
 */
export function PackageLiabilityReportTab() {
  return (
    <EmptyState
      icon={BarChart3}
      title="The package-liability report is not built yet"
      description="Outstanding package credits and what they're worth will show here."
    />
  )
}
