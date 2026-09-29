import { PackageOpen } from 'lucide-react'
import { EmptyState } from '@/components/empty-state'

/**
 * Package definitions (spec #95 user stories 57-59): create, edit, deactivate and reactivate
 * bundles of services with per-item tax toggles and a tax convention. `billing.manage`, Admin
 * Mode, the same posture every other Settings panel already documents. This file is that
 * later ticket's slot — empty until it lands.
 */
export function PackagesPanel() {
  return (
    <EmptyState
      icon={PackageOpen}
      title="Packages are not built yet"
      description="Defining bundles of services to sell as packages will happen here."
    />
  )
}
