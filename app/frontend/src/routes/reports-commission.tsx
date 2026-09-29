import { BarChart3 } from 'lucide-react'
import { EmptyState } from '@/components/empty-state'

/**
 * Commission (spec #95 user story 67): a date range and staff filter, each person's totals
 * and the lines behind them, CSV export. `commission.view`, Admin Mode. This file is that
 * later ticket's slot — empty until it lands.
 */
export function CommissionReportTab() {
  return (
    <EmptyState
      icon={BarChart3}
      title="The commission report is not built yet"
      description="Each staff member's commission totals, and the lines behind them, will show here."
    />
  )
}
