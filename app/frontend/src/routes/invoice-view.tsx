import { Receipt } from 'lucide-react'
import { EmptyState } from '@/components/empty-state'

/**
 * One invoice (spec #95 user stories 15-41): lines, discounts, tax, the override adjustment,
 * the balance breakdown, replacement lineage, payments, and the actions that act on it. This
 * file is that later ticket's slot — empty until it lands.
 */
export function InvoiceViewPage() {
  return (
    <div className="mx-auto max-w-3xl">
      <EmptyState
        icon={Receipt}
        title="The invoice view is not built yet"
        description="Lines, tax, the balance breakdown, payments and the actions on this invoice will show here."
      />
    </div>
  )
}
