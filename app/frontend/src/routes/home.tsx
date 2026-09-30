import { useQuery } from '@tanstack/react-query'
import { CalendarPlus, ClipboardList, RefreshCw } from 'lucide-react'
import { Link } from 'react-router'
import { EmptyState } from '@/components/empty-state'
import { OnboardingChecklist } from '@/components/onboarding-checklist'
import { OnboardingEmailBanner } from '@/components/onboarding-email-banner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { fetchAdminBusiness, fetchFormsNeeded, fetchHealth, type ComplianceStatus } from '@/lib/api'
import { useSession } from '@/lib/auth'
import { useBranding } from '@/lib/branding'
import { BUSINESS, FORMS_NEEDED } from '@/lib/query-keys'

export function HomePage() {
  const { user } = useSession()
  const canSeeFormsNeeded = user?.capabilities.includes('forms.issue') ?? false
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <EmptyState
        icon={CalendarPlus}
        title="Your workspace is ready"
        description="Scheduling, clients and your service catalog arrive with the next releases. Until then, this page reports whether the backend is reachable."
      />
      <OnboardingEmailBanner />
      <OnboardingChecklist />
      {canSeeFormsNeeded && <FormsNeededCard />}
      {user?.mode === 'admin' && <BusinessProfileCard />}
      <SystemStatus />
    </div>
  )
}

const FORMS_NEEDED_DAYS = 14
const STATUS_LABEL: Record<ComplianceStatus, string> = {
  missing: 'Missing',
  expired: 'Expired',
  resign_required: 'Needs a new signature',
}

/**
 * Task 8 (#51): who is coming in within the next two weeks with an essential form still
 * outstanding — the front desk's "catch it before they sit down" list. Each row opens the
 * client's profile, where the same banner (and its "Send form" shortcut) is waiting.
 */
function FormsNeededCard() {
  const branding = useBranding()
  const timezone = branding.data?.timezone
  const { data, isPending, isError, error } = useQuery({
    queryKey: [...FORMS_NEEDED, FORMS_NEEDED_DAYS],
    queryFn: () => fetchFormsNeeded(FORMS_NEEDED_DAYS),
    retry: false,
  })

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle>
          <h2>Forms needed</h2>
        </CardTitle>
        <CardDescription>Clients booked in the next {FORMS_NEEDED_DAYS} days</CardDescription>
      </CardHeader>
      <CardContent>
        {isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : isError ? (
          <p role="alert" className="text-sm text-destructive">
            {error.message}
          </p>
        ) : data.length === 0 ? (
          <EmptyState
            icon={ClipboardList}
            title="Nothing outstanding"
            description="Every client booked in the next two weeks has their essential forms up to date."
          />
        ) : (
          <ul className="divide-y">
            {data.map((entry) => (
              <li key={entry.customer_id} className="flex items-center justify-between gap-3 py-2">
                <div className="min-w-0">
                  <Link to={`/clients/${entry.customer_id}`} className="font-medium hover:underline">
                    {entry.customer_name}
                  </Link>
                  <p className="text-xs text-muted-foreground tabular-nums">
                    {new Date(entry.next_appointment_at).toLocaleString('en-CA', {
                      timeZone: timezone,
                      dateStyle: 'medium',
                      timeStyle: 'short',
                    })}
                  </p>
                </div>
                <div className="flex flex-wrap justify-end gap-1">
                  {entry.templates.map((t) => (
                    <Badge key={t.template_id} variant="warning">
                      {t.name}: {STATUS_LABEL[t.status]}
                    </Badge>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

/**
 * A read-only glance, and the proof that the guard is real: this data comes from an endpoint
 * that answers 403 to the same signed-in user in Staff Mode. Settings → Business is where the
 * same record is edited; the card stays because "am I actually administering right now?" is
 * a question the dashboard should answer without a trip into Settings.
 *
 * The name appears twice on this screen — here and in the sidebar — and that is not a
 * duplicate: the sidebar's comes from the anonymous branding document, this one from behind
 * the Admin Mode guard. The mode tests assert on this card's *title* for that reason.
 */
function BusinessProfileCard() {
  const { data, isPending, refetch, isFetching } = useQuery({
    queryKey: BUSINESS,
    queryFn: fetchAdminBusiness,
    retry: false,
  })

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle>Business profile</CardTitle>
        <CardDescription>Visible in Admin Mode only</CardDescription>
        <CardAction>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Refresh business profile"
            disabled={isFetching}
            onClick={() => refetch()}
          >
            <RefreshCw className={isFetching ? 'animate-spin' : undefined} />
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent>
        <dl className="divide-y">
          <ProfileRow label="Name" value={data?.name} loading={isPending} />
          <ProfileRow label="Timezone" value={data?.timezone} loading={isPending} />
          <ProfileRow
            label="Set up"
            value={data?.setup_completed_at ? new Date(data.setup_completed_at).toLocaleDateString() : undefined}
            loading={isPending}
          />
        </dl>
      </CardContent>
    </Card>
  )
}

function ProfileRow(props: { label: string; value?: string; loading: boolean }) {
  return (
    <div className="flex h-10 items-center justify-between">
      <dt className="font-medium">{props.label}</dt>
      <dd className="text-muted-foreground">
        {props.loading ? <Skeleton className="h-5 w-32" /> : (props.value ?? '—')}
      </dd>
    </div>
  )
}

function SystemStatus() {
  const { data, isPending, isError, refetch, isFetching } = useQuery({
    queryKey: ['health'],
    queryFn: fetchHealth,
    refetchInterval: 30_000,
  })

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle>System status</CardTitle>
        <CardDescription>Checked every 30 seconds</CardDescription>
        <CardAction>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Refresh status"
            disabled={isFetching}
            onClick={() => refetch()}
          >
            <RefreshCw className={isFetching ? 'animate-spin' : undefined} />
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent>
        <dl className="divide-y">
          <StatusRow label="API" loading={isPending} ok={!isError} okText="Online" failText="Unreachable" />
          <StatusRow
            label="Database"
            loading={isPending}
            ok={!isError && data?.database === 'ok'}
            okText="Connected"
            failText="Unreachable"
          />
        </dl>
      </CardContent>
    </Card>
  )
}

function StatusRow(props: { label: string; loading: boolean; ok: boolean; okText: string; failText: string }) {
  return (
    <div className="flex h-10 items-center justify-between">
      <dt className="font-medium">{props.label}</dt>
      <dd>
        {props.loading ? (
          <Skeleton className="h-5 w-20" />
        ) : (
          <Badge variant={props.ok ? 'success' : 'destructive'}>{props.ok ? props.okText : props.failText}</Badge>
        )}
      </dd>
    </div>
  )
}
