import { PackageOpen } from 'lucide-react'
import { EmptyState } from '@/components/empty-state'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

/**
 * A client's Packages tab (spec #95 user story 54): purchases with remaining credits per
 * service, expiry and status, plus selling and refunding a package. Behind `billing.view` —
 * `clients.tsx` is what decides whether this card renders. This file is a later ticket's
 * slot — empty until it lands.
 */
export function ClientPackagesCard(_props: { customerId: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm font-medium">Packages</CardTitle>
      </CardHeader>
      <CardContent>
        <EmptyState
          icon={PackageOpen}
          title="Packages are not built yet"
          description="This client's package purchases and remaining credits will list here."
        />
      </CardContent>
    </Card>
  )
}
