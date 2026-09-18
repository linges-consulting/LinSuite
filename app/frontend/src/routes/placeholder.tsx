import { Construction } from 'lucide-react'
import { EmptyState } from '@/components/empty-state'

export function PlaceholderPage({ title }: { title: string }) {
  return (
    <div className="mx-auto max-w-3xl">
      <EmptyState
        icon={Construction}
        title={`${title} is not built yet`}
        description="This area is planned for an upcoming release."
      />
    </div>
  )
}
