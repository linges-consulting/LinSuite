import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ListOrdered, Plus, TriangleAlert } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { EmptyState } from '@/components/empty-state'
import { Field, Form, FormError } from '@/components/form'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import {
  ApiError,
  abandonQueueEntry,
  addQueueEntry,
  fetchCatalog,
  fetchQueueEntries,
  fetchRoster,
  searchCustomers,
  startQueueEntry,
  type AddQueueEntryDraft,
  type Customer,
  type QueueEntry,
} from '@/lib/api'
import { clock, today } from '@/lib/calendar/format'
import { RULE_LABEL } from '@/lib/calendar/overrides'
import { useBranding } from '@/lib/branding'
import { formatPhone } from '@/lib/phone'
import { CATALOG, CUSTOMERS, QUEUE, ROSTER } from '@/lib/query-keys'

const ANY = 'any'

/** `can_start`'s own refusal reasons (Task 4/5, #12): `staff_busy` is this endpoint's one
 *  physical reason on top of the four advisory rules the booking dialog already labels
 *  (`lib/calendar/overrides.ts::RULE_LABEL`) — reused rather than a second copy of the same
 *  four words. */
const START_REASON_LABEL: Record<string, string> = {
  staff_busy: 'busy right now',
  ...RULE_LABEL,
}

/**
 * Front-desk "take a number" display (Phase 7 Task 8, #12): who is waiting or already being
 * seen, the wait estimate (Task 6 — an estimate, never a promise, said so right on the
 * screen), and the essential-form gap indicator (Task 7). Reached only when
 * `enable_walk_in_queue` is on and the account holds `queue.manage` — both already decide
 * whether the nav link to this screen exists at all (`lib/nav.ts`); a direct visit without
 * the toggle gets the same honest "turned off" state below, off the identical 404 the list
 * endpoint itself sends.
 */
export function QueuePage() {
  const [adding, setAdding] = useState(false)
  const branding = useBranding()
  const zone = branding.data?.timezone
  const queue = useQuery({
    queryKey: QUEUE,
    queryFn: () => fetchQueueEntries(true),
    // Live, not a snapshot: Task 6/7's own wait-estimate/compliance-gap fields are
    // recomputed fresh on every read, so a display screen has to keep asking.
    refetchInterval: 20_000,
  })

  if (queue.isPending) return <Skeleton className="h-64 w-full" />

  if (queue.data === null) {
    return (
      <EmptyState
        icon={ListOrdered}
        title="Walk-in queue is turned off"
        description='Turn on "Walk-in queue" in Settings → Notifications to use this screen.'
      />
    )
  }

  if (queue.isError) {
    const message =
      queue.error instanceof ApiError && queue.error.status === 403
        ? "You don't have permission to see the queue."
        : queue.error.message
    return (
      <p role="alert" className="text-destructive">
        {message}
      </p>
    )
  }

  const entries = queue.data.entries
  const waiting = entries.filter((e) => e.status === 'waiting')
  const inService = entries.filter((e) => e.status === 'in_service')
  const visible = [...waiting, ...inService]
  // "Abandoned, and countable" (Task 8's own criterion) — the same list this request already
  // fetched (`?include_abandoned=true`), filtered to today rather than a second backend
  // query. Compared against the business's own calendar day when it is known; falls back to
  // every abandoned entry ever (a superset, never wrong in the other direction) for the one
  // request before branding's timezone is cached — in practice already warm by the time this
  // screen is reachable, since `useApplyBranding` fetches it at app boot.
  const abandonedToday = entries.filter(
    (e) => e.status === 'abandoned' && (!zone || today(zone, new Date(e.arrived_at)) === today(zone)),
  ).length

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-muted-foreground tabular-nums" aria-live="polite">
          {waiting.length} waiting · {inService.length} in service · {abandonedToday} abandoned today
        </p>
        <Button size="sm" className="ml-auto" onClick={() => setAdding(true)}>
          <Plus aria-hidden />
          Add walk-in
        </Button>
      </div>

      {adding && <AddQueueEntryDialog onClose={() => setAdding(false)} />}

      {visible.length === 0 ? (
        <EmptyState
          icon={ListOrdered}
          title="Nobody waiting"
          description="Walk-ins added to the queue show up here."
        />
      ) : (
        <div className="space-y-2">
          {/* Task 7's own documented gap, said plainly rather than implied by a clean list:
              a service-specific essential form can't yet know it applies to a walk-in with no
              confirmed appointment, so only a form required of every client is guaranteed to
              show here before the visit is booked. */}
          <p className="text-xs text-muted-foreground">
            "Forms" only reliably shows gaps for essential forms required of every client — one
            tied to this specific service may not appear until after the visit is booked.
          </p>
          <div className="rounded-xl border bg-card">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-4">Client</TableHead>
                  <TableHead>Service</TableHead>
                  <TableHead>Preferred staff</TableHead>
                  <TableHead>Arrived</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Forms</TableHead>
                  <TableHead className="pr-4 text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visible.map((entry) => (
                  <QueueRow key={entry.id} entry={entry} zone={zone} />
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      )}
    </div>
  )
}

function QueueRow({ entry, zone }: { entry: QueueEntry; zone?: string }) {
  const queryClient = useQueryClient()
  const name = entry.customer ? `${entry.customer.first_name} ${entry.customer.last_name}` : entry.bare_name
  const phone = entry.customer?.phone ?? entry.bare_phone
  const gaps = entry.compliance_gaps ?? []

  const abandon = useMutation({
    mutationFn: () => abandonQueueEntry(entry.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUEUE })
      toast.success(`${name} marked as gone`)
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : 'Could not update this entry'),
  })

  const start = useMutation({
    mutationFn: () => startQueueEntry(entry.id),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: QUEUE })
      toast.success(`${name} started with ${result.appointment.staff.display_name}`)
    },
    onError: (error) => {
      // `not_eligible` (`can_start` refusing every staff candidate — Task 4/5's own literal
      // acceptance criterion, "a walk-in cannot be started if it would run into a booked
      // appointment") and `resource_unavailable` (a busy room/device `can_start` never checks,
      // Task 5's own separate 422) are two different reasons, worded differently — not one
      // generic failure for both.
      if (error instanceof ApiError && error.code === 'not_eligible') {
        const reason = (error.body as { reason?: string } | null)?.reason
        toast.error(
          `Can't start yet — ${reason ? (START_REASON_LABEL[reason] ?? reason) : 'not eligible right now'}.`,
        )
      } else if (error instanceof ApiError && error.code === 'resource_unavailable') {
        toast.error(error.message)
      } else {
        toast.error(error instanceof Error ? error.message : 'Could not start this walk-in')
      }
      // A refusal can mean somebody else already acted on this entry (raced abandon/start) —
      // reload so the row shows what actually happened rather than a stale "waiting".
      queryClient.invalidateQueries({ queryKey: QUEUE })
    },
  })

  return (
    <TableRow className="h-12">
      <TableCell className="pl-4">
        <span className="font-medium">{name}</span>
        {phone && <span className="ml-2 tabular-nums text-muted-foreground">{formatPhone(phone)}</span>}
      </TableCell>
      <TableCell>{entry.requested_service.name}</TableCell>
      <TableCell>{entry.preferred_staff?.display_name ?? 'Any available'}</TableCell>
      <TableCell className="tabular-nums">{clock(entry.arrived_at, zone)}</TableCell>
      <TableCell>
        <Badge variant={entry.status === 'in_service' ? 'info' : 'outline'}>
          {entry.status === 'in_service' ? 'In service' : 'Waiting'}
        </Badge>
        {entry.status === 'waiting' && entry.estimated_wait_minutes !== null && (
          <p className="text-xs text-muted-foreground">~{entry.estimated_wait_minutes} min wait (estimate)</p>
        )}
      </TableCell>
      <TableCell>
        {gaps.length > 0 ? (
          <Badge variant="warning" title={gaps.map((g) => `${g.name}: ${g.status}`).join(', ')}>
            <TriangleAlert aria-hidden />
            {gaps.length === 1 ? '1 form' : `${gaps.length} forms`}
          </Badge>
        ) : (
          <span className="text-xs text-muted-foreground">—</span>
        )}
      </TableCell>
      <TableCell className="pr-4 text-right">
        {entry.status === 'waiting' && (
          <div className="flex justify-end gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={abandon.isPending || start.isPending}
              onClick={() => abandon.mutate()}
            >
              Abandon
            </Button>
            <Button size="sm" disabled={abandon.isPending || start.isPending} onClick={() => start.mutate()}>
              {start.isPending ? 'Starting…' : 'Start'}
            </Button>
          </div>
        )}
      </TableCell>
    </TableRow>
  )
}

/**
 * Quick-create (Task 2's own shape): a name is enough, phone optional, no full customer
 * record forced — "a walk-in may never become a returning client" (CLAUDE.md). Defaults to
 * the walk-in identity rather than the existing-client search the booking dialog defaults
 * to, since that is the common case here.
 */
function AddQueueEntryDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient()
  const catalog = useQuery({ queryKey: CATALOG, queryFn: fetchCatalog })
  const roster = useQuery({ queryKey: ROSTER, queryFn: fetchRoster, retry: false })
  const [existing, setExisting] = useState(false)
  const [search, setSearch] = useState('')
  const [customer, setCustomer] = useState<Customer | null>(null)
  const [bareName, setBareName] = useState('')
  const [barePhone, setBarePhone] = useState('')
  const [serviceId, setServiceId] = useState('')
  const [staffId, setStaffId] = useState(ANY)

  const matches = useQuery({
    queryKey: [...CUSTOMERS, search],
    queryFn: () => searchCustomers(search),
    enabled: existing && search.trim().length > 0,
  })

  const service = catalog.data?.find((s) => s.id === serviceId)
  // A custom role holding `queue.manage` without `schedule.view` sees this fetch fail — the
  // preferred-staff picker just falls back to "Any available" only, never a broken dialog:
  // `preferred_staff_id` is optional either way.
  const providers = (roster.data ?? []).filter((m) => service?.staff_ids.includes(m.id))

  const chooseService = (id: string) => {
    setServiceId(id)
    const eligible = catalog.data?.find((s) => s.id === id)?.staff_ids ?? []
    if (!eligible.includes(staffId)) setStaffId(ANY)
  }

  const add = useMutation({
    mutationFn: () => {
      const draft: AddQueueEntryDraft = {
        requested_service_id: serviceId,
        preferred_staff_id: staffId === ANY ? null : staffId,
      }
      if (existing) draft.customer_id = (customer as Customer).id
      else {
        draft.bare_name = bareName.trim()
        draft.bare_phone = barePhone.trim() || null
      }
      return addQueueEntry(draft)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUEUE })
      toast.success('Added to the queue')
      onClose()
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : 'Could not add this walk-in'),
  })

  const identityReady = existing ? customer !== null : bareName.trim() !== ''
  const ready = serviceId !== '' && identityReady

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add to the queue</DialogTitle>
          <DialogDescription>
            A name is enough — a full client record isn't required for a walk-in.
          </DialogDescription>
        </DialogHeader>
        <Form onSubmit={() => ready && add.mutate()}>
          <Field label="Service" htmlFor="queue-service">
            <Select value={serviceId} onValueChange={chooseService}>
              <SelectTrigger id="queue-service" aria-label="Service" className="w-full">
                <SelectValue placeholder="Choose a service" />
              </SelectTrigger>
              <SelectContent>
                {(catalog.data ?? []).map((s) => (
                  <SelectItem key={s.id} value={s.id}>
                    {s.name} · {s.duration_minutes} min
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <Field label="Preferred staff" htmlFor="queue-staff">
            <Select value={staffId} onValueChange={setStaffId} disabled={!service}>
              <SelectTrigger id="queue-staff" aria-label="Preferred staff" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ANY}>Any available</SelectItem>
                {providers.map((m) => (
                  <SelectItem key={m.id} value={m.id}>
                    {m.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <fieldset className="flex flex-col gap-2">
            <legend className="mb-2 text-sm font-medium">Client</legend>
            <div className="flex gap-2">
              <Button
                type="button"
                size="sm"
                variant={existing ? 'outline' : 'default'}
                aria-pressed={!existing}
                onClick={() => setExisting(false)}
              >
                Walk-in
              </Button>
              <Button
                type="button"
                size="sm"
                variant={existing ? 'default' : 'outline'}
                aria-pressed={existing}
                onClick={() => setExisting(true)}
              >
                Existing client
              </Button>
            </div>
            {existing ? (
              customer ? (
                <div className="flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-sm">
                  <span className="font-medium">
                    {customer.first_name} {customer.last_name}
                  </span>
                  <Button type="button" variant="ghost" size="sm" onClick={() => setCustomer(null)}>
                    Change
                  </Button>
                </div>
              ) : (
                <>
                  <Input
                    aria-label="Find a client"
                    placeholder="Name, phone or email"
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                  />
                  {matches.data && matches.data.length === 0 && (
                    <p className="text-xs text-muted-foreground">
                      Nobody matches. Add them as a walk-in instead.
                    </p>
                  )}
                  {matches.data && matches.data.length > 0 && (
                    <ul className="flex flex-col divide-y rounded-lg border">
                      {matches.data.map((c) => (
                        <li key={c.id}>
                          <button
                            type="button"
                            className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm hover:bg-accent"
                            onClick={() => setCustomer(c)}
                          >
                            <span className="font-medium">
                              {c.first_name} {c.last_name}
                            </span>
                            <span className="tabular-nums text-muted-foreground">
                              {c.phone ? formatPhone(c.phone) : (c.email ?? '')}
                            </span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              )
            ) : (
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Name" htmlFor="queue-bare-name">
                  <Input
                    id="queue-bare-name"
                    autoFocus
                    required
                    maxLength={200}
                    value={bareName}
                    onChange={(e) => setBareName(e.target.value)}
                  />
                </Field>
                <Field label="Phone (optional)" htmlFor="queue-bare-phone">
                  <Input
                    id="queue-bare-phone"
                    value={barePhone}
                    onChange={(e) => setBarePhone(e.target.value)}
                  />
                </Field>
              </div>
            )}
          </fieldset>

          {add.error && <FormError>{add.error.message}</FormError>}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={add.isPending || !ready}>
              {add.isPending ? 'Adding…' : 'Add to queue'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
