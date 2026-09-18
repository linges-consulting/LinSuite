import { useQuery } from '@tanstack/react-query'
import { CalendarPlus, RefreshCw } from 'lucide-react'
import { EmptyState } from '@/components/empty-state'
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
import { fetchHealth } from '@/lib/api'

export function HomePage() {
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <EmptyState
        icon={CalendarPlus}
        title="Your workspace is ready"
        description="Scheduling, clients and your service catalog arrive with the next releases. Until then, this page reports whether the backend is reachable."
      />
      <SystemStatus />
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
