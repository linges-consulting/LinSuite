import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Circle } from 'lucide-react'
import { Link } from 'react-router'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { dismissOnboarding, fetchOnboardingStatus } from '@/lib/api'
import { useCan } from '@/lib/capability-gate'
import { STEP_LABEL } from '@/lib/onboarding-steps'
import { ONBOARDING } from '@/lib/query-keys'

/**
 * "Get your business ready" (#116, spec #113): the eight-step setup checklist, Admin Mode,
 * `admin` capability only — `useCan('admin')` covers both and this renders nothing at all
 * otherwise, the same "absent, never disabled" rule every other Admin-only surface here follows
 * (`lib/capability-gate.ts`). Nothing fetches until that gate passes, so a Staff Mode session or
 * an account without the capability never even asks the server.
 *
 * Dismissal is shared (spec user story 9: dismissed for the owner is dismissed for every
 * administrator) — the server sets one timestamp on the business row, not per-account state,
 * so this stays hidden for anyone reloading afterwards without polling.
 */
export function OnboardingChecklist() {
  const canAdmin = useCan('admin')
  const queryClient = useQueryClient()
  const { data, isPending } = useQuery({
    queryKey: ONBOARDING,
    queryFn: fetchOnboardingStatus,
    enabled: canAdmin,
    retry: false,
  })
  const dismiss = useMutation({
    mutationFn: dismissOnboarding,
    onSuccess: (status) => queryClient.setQueryData(ONBOARDING, status),
  })

  if (!canAdmin) return null
  if (isPending) return <Skeleton className="h-48 w-full" />
  if (!data?.steps?.length || data.dismissed_at) return null

  const done = data.steps.filter((s) => s.done).length
  const total = data.steps.length

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle>Get your business ready</CardTitle>
        <CardDescription>
          {done} of {total} steps done
        </CardDescription>
        <CardAction>
          <Button variant="ghost" size="sm" onClick={() => dismiss.mutate()} disabled={dismiss.isPending}>
            Dismiss
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent>
        <div
          role="progressbar"
          aria-valuenow={done}
          aria-valuemin={0}
          aria-valuemax={total}
          aria-label="Setup progress"
          className="mb-4 h-2 w-full overflow-hidden rounded-full bg-muted"
        >
          <div
            className="h-full rounded-full bg-primary transition-all"
            style={{ width: `${(done / total) * 100}%` }}
          />
        </div>
        <ul className="divide-y">
          {data.steps.map((s) => (
            <li key={s.key} className="flex items-center justify-between gap-3 py-2">
              <Link to={`/setup-checklist/${s.key}`} className="flex items-center gap-2 hover:underline">
                {s.done ? (
                  <CheckCircle2 className="size-4 shrink-0 text-success" aria-hidden />
                ) : (
                  <Circle className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                )}
                {STEP_LABEL[s.key]}
              </Link>
              {s.optional && <Badge variant="secondary">Optional</Badge>}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  )
}
