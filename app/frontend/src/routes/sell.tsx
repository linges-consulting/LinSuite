import { ShoppingCart } from 'lucide-react'
import { EmptyState } from '@/components/empty-state'

/**
 * Retail checkout (spec #95 user stories 42-50): start a sale with or without a client, add
 * products, apply a discount, issue and take payment in one flow. Replaces the empty Catalog
 * placeholder (#97) — this file is that later ticket's slot, empty until it lands.
 */
export function SellPage() {
  return (
    <div className="mx-auto max-w-3xl">
      <EmptyState
        icon={ShoppingCart}
        title="Sell is not built yet"
        description="Starting a sale, adding products and taking payment will happen here."
      />
    </div>
  )
}
