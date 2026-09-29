import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { BillsPage } from '@/routes/bills'
import { InvoicesTab } from '@/routes/invoices'

/**
 * Billing (renamed from Bills, #97/#95): the draft bills waiting for review, and — once #95's
 * own ticket lands — the invoices issued from them. `To review` is exactly the screen `/bills`
 * always was (`BillsPage`, unchanged); this file only adds the tab shell around it and the
 * `Invoices` slot beside it, so that later ticket edits `routes/invoices.tsx` rather than this
 * one or `routes/bills.tsx`.
 */
export function BillingPage() {
  return (
    <Tabs defaultValue="to-review" className="gap-4">
      <TabsList>
        <TabsTrigger value="to-review">To review</TabsTrigger>
        <TabsTrigger value="invoices">Invoices</TabsTrigger>
      </TabsList>
      <TabsContent value="to-review">
        <BillsPage />
      </TabsContent>
      <TabsContent value="invoices">
        <InvoicesTab />
      </TabsContent>
    </Tabs>
  )
}
