import { ShieldCheck } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { EmptyState } from '@/components/empty-state'
import { useSession } from '@/lib/auth'

/**
 * The plain refusal page itself, factored out so `RequireAdminMode` below and the focused
 * checklist step pages (`routes/setup-checklist.tsx`, #117) — which gate on the `admin`
 * capability itself rather than only the mode — can show the identical screen instead of two
 * copies of the same JSX drifting apart.
 */
export function AdminModeRequiredNotice() {
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

/**
 * The route-level half of Staff Mode hiding (#114, spec #113): a route whose entire purpose
 * is administrative — Settings, Reports — never partially renders in Staff Mode. Typing the
 * URL directly gets this page, pointing at the header's mode switcher (the one control that
 * gets you past it, already on screen), rather than a screen that half-loads and then refuses
 * each thing on it one at a time.
 *
 * **Sticky once entered.** That rule is about arriving in Staff Mode, not about losing Admin
 * Mode after the page is already up. `auth/modes.py` slides the idle window only on an actual
 * admin request, and nothing here makes one while the tab is backgrounded — `refetchInterval`
 * pauses off-screen and `/auth/me` itself never slides the window (that file's own docstring).
 * So an administrator who switches away to fetch an API key or read a code, and comes back
 * after the window has quietly lapsed, produced a focus refetch that flips `user.mode` to
 * `'staff'` with nothing else having changed — and unmounting `children` on that alone threw
 * away whatever was half-typed underneath, which is exactly the "screen that empties on expiry
 * looks broken rather than locked" `SettingsPage` already says this area must not do. Once
 * this mount has seen Admin Mode, it keeps rendering `children` regardless of what the session
 * says afterwards; every write is still enforced server-side (`Requires("admin")`), and a
 * refused one surfaces through the ordinary 403 toast, not through this guard.
 */
export function RequireAdminMode({ children }: { children: ReactNode }) {
  const { user } = useSession()
  const [entered, setEntered] = useState(user?.mode === 'admin')
  if (!entered && user?.mode === 'admin') setEntered(true)
  if (!entered) return <AdminModeRequiredNotice />
  return <>{children}</>
}
