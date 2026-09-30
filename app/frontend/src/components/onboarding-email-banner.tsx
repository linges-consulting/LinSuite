import { useQuery } from '@tanstack/react-query'
import { MailWarning } from 'lucide-react'
import { fetchOnboardingStatus } from '@/lib/api'
import { useCan } from '@/lib/capability-gate'
import { ONBOARDING } from '@/lib/query-keys'

/**
 * "Emails are not being sent — they go to the server log" (#116, spec #113): a Home banner in
 * Admin Mode, independent of the checklist's own dismissal — spec user story 30, "that warning
 * to stay until a sender is verified, even after dismissing the checklist". It reads the same
 * onboarding status the checklist does (one query, one cache entry — `ONBOARDING`), rather than
 * a second endpoint, since "a sender is configured and its latest test send succeeded" is
 * exactly the checklist's own `email` step.
 *
 * Admin Mode, `admin` capability only, the same `useCan` gate the checklist uses — Staff Mode
 * never sees this, matching spec user story 11 ("Home unchanged by setup" for the front desk).
 */
export function OnboardingEmailBanner() {
  const canAdmin = useCan('admin')
  const { data } = useQuery({
    queryKey: ONBOARDING,
    queryFn: fetchOnboardingStatus,
    enabled: canAdmin,
    retry: false,
  })

  if (!canAdmin || !data?.steps?.length) return null
  const email = data.steps.find((s) => s.key === 'email')
  if (!email || email.done) return null

  return (
    <div
      role="alert"
      className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm"
    >
      <MailWarning className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
      <p className="font-medium">Emails are not being sent — they go to the server log</p>
    </div>
  )
}
