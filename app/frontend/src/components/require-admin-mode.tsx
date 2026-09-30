import { ShieldCheck } from 'lucide-react'
import type { ReactNode } from 'react'
import { EmptyState } from '@/components/empty-state'
import { useSession } from '@/lib/auth'

/**
 * The route-level half of Staff Mode hiding (#114, spec #113): a route whose entire purpose
 * is administrative — Settings, Reports — never partially renders in Staff Mode. Typing the
 * URL directly gets this page, pointing at the header's mode switcher (the one control that
 * gets you past it, already on screen), rather than a screen that half-loads and then refuses
 * each thing on it one at a time.
 */
export function RequireAdminMode({ children }: { children: ReactNode }) {
  const { user } = useSession()
  if (user?.mode !== 'admin') {
    return (
      <div className="mx-auto flex max-w-md flex-col items-center gap-4 py-12 text-center">
        <EmptyState
          icon={ShieldCheck}
          title="This area needs Admin Mode"
          description="Use the mode switcher at the top of the page to continue."
        />
      </div>
    )
  }
  return <>{children}</>
}
