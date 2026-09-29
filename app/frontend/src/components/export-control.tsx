import { useMutation, useQuery } from '@tanstack/react-query'
import { Download, Loader2, TriangleAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { ApiError, type ExportJob } from '@/lib/api'

const POLL_INTERVAL_MS = 2000

export type ExportControlProps = {
  /** Requests a new export for the caller's own params (date range, staff, client…) — the
   *  control itself knows nothing about what it is exporting. */
  requestExport: () => Promise<ExportJob>
  /** Polls one export's status by id, the same shape `requestExport` answered. */
  pollExport: (id: string) => Promise<ExportJob>
  /** Fetches the ready file and saves it. Rejects with an `ApiError` — 410 once the export
   *  has expired — exactly like every other call in `lib/api.ts`. */
  downloadExport: (id: string) => Promise<void>
  /** The idle/retry button's label. Defaults to what every kind calls this action. */
  label?: string
}

/**
 * One export control for every CSV export (#86/#95's shared mechanism, #98): idle → request
 * → preparing (polled with `refetchInterval` until `ready`/`failed`) → download, with a 410
 * on download read as "expired" rather than a generic failure. Parameterised by the three
 * calls a report kind makes around `core/exports.py` — the client access-log panel is the
 * first caller (#92); commission and package-liability reuse this unchanged, only their
 * `requestExport`/`pollExport`/`downloadExport` differ.
 */
export function ExportControl({ requestExport, pollExport, downloadExport, label = 'Export CSV' }: ExportControlProps) {
  const [job, setJob] = useState<ExportJob | null>(null)
  const [expired, setExpired] = useState(false)

  const request = useMutation({
    mutationFn: requestExport,
    onSuccess: (data) => {
      setJob(data)
      setExpired(false)
    },
  })

  const poll = useQuery({
    queryKey: ['export-control-poll', job?.id],
    queryFn: () => pollExport(job!.id),
    enabled: job?.status === 'pending',
    refetchInterval: (query) => (query.state.data?.status === 'pending' ? POLL_INTERVAL_MS : false),
  })

  useEffect(() => {
    if (poll.data && poll.data.id === job?.id) setJob(poll.data)
    // job.id alone would refire this on every poll.data identity change for a stale job.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [poll.data])

  const download = useMutation({
    mutationFn: () => downloadExport(job!.id),
    onError: (error) => {
      if (error instanceof ApiError && error.status === 410) setExpired(true)
    },
  })

  if (expired) {
    return (
      <div className="flex items-center gap-2">
        <span role="alert" className="text-sm text-destructive">
          This export has expired.
        </span>
        <Button type="button" variant="outline" size="sm" disabled={request.isPending} onClick={() => request.mutate()}>
          Request a new export
        </Button>
      </div>
    )
  }

  if (job?.status === 'failed') {
    return (
      <div className="flex items-center gap-2">
        <span role="alert" className="flex items-center gap-1 text-sm text-destructive">
          <TriangleAlert className="size-4" aria-hidden /> The export failed.
        </span>
        <Button type="button" variant="outline" size="sm" disabled={request.isPending} onClick={() => request.mutate()}>
          Try again
        </Button>
      </div>
    )
  }

  if (job?.status === 'pending') {
    return (
      <span className="flex items-center gap-2 text-sm text-muted-foreground" aria-live="polite">
        <Loader2 className="size-4 animate-spin" aria-hidden /> Preparing…
      </span>
    )
  }

  if (job?.status === 'ready') {
    return (
      <Button type="button" variant="outline" size="sm" disabled={download.isPending} onClick={() => download.mutate()}>
        <Download className="size-4" aria-hidden /> {download.isPending ? 'Downloading…' : 'Download CSV'}
      </Button>
    )
  }

  return (
    <Button type="button" variant="outline" size="sm" disabled={request.isPending} onClick={() => request.mutate()}>
      {request.isPending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Download className="size-4" aria-hidden />}
      {label}
    </Button>
  )
}
