import { Receipt } from 'lucide-react'
import { EmptyState } from '@/components/empty-state'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

/**
 * A client's Invoices tab (spec #95 user story 65): both service and retail invoices, so
 * their whole billing history is in one place. Behind `billing.view`, the same front-desk
 * capability as Billing itself — `clients.tsx` is what decides whether this card renders.
 * This file is a later ticket's slot — empty until it lands.
 */
export function ClientInvoicesCard(_props: { customerId: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm font-medium">Invoices</CardTitle>
      </CardHeader>
      <CardContent>
        <EmptyState
          icon={Receipt}
          title="Invoices are not built yet"
          description="This client's service and retail invoices will list here."
        />
      </CardContent>
    </Card>
  )
}
