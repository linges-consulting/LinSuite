import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarDays, ChevronLeft, ChevronRight, Plus } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { CancelConfirm } from '@/components/calendar/cancel-confirm'
import { Grid } from '@/components/calendar/grid'
import { OverrideConfirm } from '@/components/calendar/override-confirm'
import type { Change, Column, Prefill } from '@/components/calendar/types'
import { EmptyState } from '@/components/empty-state'
import { Field, Form, FormError } from '@/components/form'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import {
  ApiError,
  bookAppointment,
  bookGroup,
  cancelAppointment,
  cancelGroup,
  changeAppointment,
  completeAppointment,
  fetchAvailability,
  fetchCatalog,
  fetchGroupAvailability,
  fetchRoster,
  fetchSchedule,
  markNoShow,
  overridableRules,
  refusedLinkIndex,
  searchCustomers,
  type Appointment,
  type AvailabilitySlot,
  type BookingDraft,
  type Customer,
  type GroupLinkDraft,
  type GroupSlot,
  type Override,
  type OverrideRule,
  type RosterEntry,
  type Schedule,
} from '@/lib/api'
import { useSession } from '@/lib/auth'
import { useBranding } from '@/lib/branding'
import { clock, today, weekdayLabel } from '@/lib/calendar/format'
import { describeRules, whyNotOverride } from '@/lib/calendar/overrides'
import { addDays, localDate } from '@/lib/calendar/pixels'
import { formatPhone } from '@/lib/phone'
import { APPOINTMENTS, AVAILABILITY, CATALOG, CUSTOMERS, ROSTER, SCHEDULE } from '@/lib/query-keys'
import { useTheme } from '@/lib/theme'

/**
 * The schedule: the grid a business runs on (tech-stack §13), and the way to put an
 * appointment on it. `components/calendar/` draws and drags; this page decides what the
 * columns are, reads the one compound document the grid needs (`/api/schedule`), and owns
 * the two writes — booking through the dialog, and moving or resizing through a drag.
 *
 * **Two views, one engine.** The day view is one column per staff member working that day
 * (or with something booked on it — a person on their day off with an appointment still
 * needs to be seen); the week view is seven date columns, for everybody or for one person.
 *
 * **A drag is optimistic, and honest about losing.** The card lands where it was dropped
 * before the server answers; if the answer is `not_offered` or `slot_taken` it goes back
 * where it was and a toast says which. The server re-runs the engine on every move, so a
 * client that snapped to the wrong place is corrected, never trusted.
 *
 * **An advisory refusal is a question, not a loss.** `override_available` names the rules
 * a human may set aside (tech-stack §22); the card stays where it was dropped while the
 * confirm dialog asks, Confirm resubmits with `override: true`, Cancel puts it back. Who may
 * confirm is the server's decision; the dialog only leaves out a button that would be
 * refused, and says who could press it.
 *
 * **"Today" is the business's today.** The zone comes from the branding document the shell
 * has already read, so the first request this screen makes is for the right day — a
 * receptionist checking from home at 23:30 in another zone sees the day the clinic is in.
 */
export function SchedulePage() {
  const branding = useBranding()
  const zone = branding.data?.timezone
  const { user } = useSession()
  const canManage = user?.capabilities?.includes('schedule.manage') ?? false
  const queryClient = useQueryClient()
  const [chosen, setChosen] = useState<string | null>(null)
  const [view, setView] = useState<'day' | 'week'>('day')
  const [staffFilter, setStaffFilter] = useState<string>(EVERYONE)
  const [booking, setBooking] = useState<Prefill | 'blank' | null>(null)
  const [override, setOverride] = useState<PendingMove | null>(null)
  const [showCancelled, setShowCancelled] = useState(false)
  const [cancelling, setCancelling] = useState<CancelTarget | null>(null)
  const roster = useQuery({ queryKey: ROSTER, queryFn: fetchRoster })
  const ownStaffId = roster.data?.find((m) => m.user_id === user?.id)?.id ?? null
  // Until the zone is known there is no "today" to ask for.
  const date = chosen ?? (zone ? today(zone) : null)
  const range = date ? (view === 'day' ? [date, date] : weekOf(date)) : null
  const filter = view === 'week' && staffFilter !== EVERYONE ? staffFilter : undefined
  const key = range ? [...SCHEDULE, range[0], range[1], filter ?? null, showCancelled] : SCHEDULE
  const schedule = useQuery({
    queryKey: key,
    queryFn: () =>
      fetchSchedule({
        from: range![0],
        to: range![1],
        staff_id: filter,
        include_cancelled: showCancelled,
      }),
    enabled: range !== null,
    // Stepping a day is a different query; keep the grid up rather than flashing a skeleton.
    placeholderData: (previous) => previous,
  })

  const refreshAfterStatusChange = () => {
    queryClient.invalidateQueries({ queryKey: SCHEDULE })
    queryClient.invalidateQueries({ queryKey: APPOINTMENTS })
    queryClient.invalidateQueries({ queryKey: AVAILABILITY })
  }
  const complete = useMutation({
    mutationFn: completeAppointment,
    onSuccess: (a) => toast.success(`Completed ${a.customer.first_name} ${a.customer.last_name}`),
    onError: (error) => toast.error(`Not completed: ${error.message}`),
    onSettled: refreshAfterStatusChange,
  })
  const noShow = useMutation({
    mutationFn: markNoShow,
    onSuccess: (a) => toast.success(`Marked ${a.customer.first_name} ${a.customer.last_name} a no-show`),
    onError: (error) => toast.error(`Not marked: ${error.message}`),
    onSettled: refreshAfterStatusChange,
  })
  const cancelOne = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string | null }) => cancelAppointment(id, reason),
    onSuccess: (a) => toast.success(`Cancelled ${a.customer.first_name} ${a.customer.last_name}`),
    onError: (error) => toast.error(`Not cancelled: ${error.message}`),
    onSettled: refreshAfterStatusChange,
  })
  const cancelWholeGroup = useMutation({
    mutationFn: ({ groupId, reason }: { groupId: string; reason: string | null }) =>
      cancelGroup(groupId, reason),
    onSuccess: () => toast.success('Visit cancelled'),
    onError: (error) => toast.error(`Not cancelled: ${error.message}`),
    onSettled: refreshAfterStatusChange,
  })

  const change = useMutation({
    mutationFn: ({ appointment, change }: MoveVariables) => changeAppointment(appointment.id, change),
    onMutate: async ({ appointment, change, before }) => {
      await queryClient.cancelQueries({ queryKey: key })
      // A confirmed override carries the picture from before the first attempt, so that a
      // refusal now puts the card back where it started rather than where it was asked to go.
      const snapshot = before ?? queryClient.getQueryData<Schedule>(key)
      queryClient.setQueryData<Schedule>(key, (current) =>
        current && {
          ...current,
          appointments: current.appointments.map((a) =>
            a.id === appointment.id ? moved(a, change) : a,
          ),
        },
      )
      return { before: snapshot }
    },
    onError: (error, variables, context) => {
      const rules = overridableRules(error)
      if (rules) {
        // The card stays put while the question is asked; Cancel is what puts it back.
        setOverride({ ...variables, rules, before: context?.before })
        return
      }
      if (context?.before) queryClient.setQueryData(key, context.before)
      toast.error(`Not moved: ${error.message}`, { duration: 8000 })
    },
    onSettled: (_moved, error) => {
      if (overridableRules(error)) return
      queryClient.invalidateQueries({ queryKey: SCHEDULE })
      queryClient.invalidateQueries({ queryKey: APPOINTMENTS })
      queryClient.invalidateQueries({ queryKey: AVAILABILITY })
    },
  })

  if (!date || !range || roster.isPending || schedule.isPending) {
    return <Skeleton className="h-[calc(100dvh-11.5rem)] w-full" />
  }
  const failed = [branding, roster, schedule].find((q) => q.isError)
  if (failed) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {failed.error?.message}
      </p>
    )
  }

  const data = schedule.data!
  const todayDate = today(data.timezone)
  const shift = (days: number) => setChosen(addDays(date, days))
  const columns = columnsFor(view, data, range, todayDate, filter ?? null)

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            size="icon-sm"
            aria-label={view === 'day' ? 'Previous day' : 'Previous week'}
            onClick={() => shift(view === 'day' ? -1 : -7)}
          >
            <ChevronLeft />
          </Button>
          <Input
            type="date"
            aria-label="Day"
            className="w-40"
            value={date}
            onChange={(e) => e.target.value && setChosen(e.target.value)}
          />
          <Button
            variant="outline"
            size="icon-sm"
            aria-label={view === 'day' ? 'Next day' : 'Next week'}
            onClick={() => shift(view === 'day' ? 1 : 7)}
          >
            <ChevronRight />
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setChosen(null)}>
            Today
          </Button>
          <Tabs value={view} onValueChange={(v) => setView(v as 'day' | 'week')}>
            <TabsList aria-label="View">
              <TabsTrigger value="day">Day</TabsTrigger>
              <TabsTrigger value="week">Week</TabsTrigger>
            </TabsList>
          </Tabs>
          {view === 'week' && (
            <Select value={staffFilter} onValueChange={setStaffFilter}>
              <SelectTrigger aria-label="Staff member" className="w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={EVERYONE}>Everyone</SelectItem>
                {(roster.data ?? []).map((m) => (
                  <SelectItem key={m.id} value={m.id}>
                    {m.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <label className="flex items-center gap-1.5 text-sm text-muted-foreground">
            <Checkbox
              checked={showCancelled}
              onCheckedChange={(checked) => setShowCancelled(checked === true)}
            />
            Show cancelled
          </label>
        </div>
        <Button onClick={() => setBooking('blank')} disabled={!canManage || columns.length === 0}>
          <Plus aria-hidden />
          New appointment
        </Button>
      </div>

      {data.staff.length === 0 ? (
        <EmptyState
          icon={CalendarDays}
          title="Nobody on the schedule yet"
          description="Staff members appear here as columns once they are added in Settings."
        />
      ) : (
        <Grid
          schedule={data}
          columns={columns}
          canManage={canManage}
          onCreate={setBooking}
          onChange={(appointment, next) => change.mutate({ appointment, change: next })}
          onComplete={(a) => complete.mutate(a.id)}
          onNoShow={(a) => noShow.mutate(a.id)}
          onCancel={(a) => setCancelling({ kind: 'appointment', appointment: a })}
          onCancelGroup={(a) =>
            a.booking_group_id && setCancelling({ kind: 'group', groupId: a.booking_group_id })
          }
        />
      )}

      {cancelling && (
        <CancelConfirm
          title={cancelling.kind === 'group' ? 'Cancel this visit?' : 'Cancel this appointment?'}
          description={
            cancelling.kind === 'group'
              ? 'Every confirmed service in this visit is cancelled; anything already completed stays that way.'
              : 'This frees its time and any room or equipment it was holding.'
          }
          pending={cancelOne.isPending || cancelWholeGroup.isPending}
          onConfirm={(reason) => {
            if (cancelling.kind === 'group') {
              cancelWholeGroup.mutate({ groupId: cancelling.groupId, reason })
            } else {
              cancelOne.mutate({ id: cancelling.appointment.id, reason })
            }
            setCancelling(null)
          }}
          onCancel={() => setCancelling(null)}
        />
      )}

      {booking && (
        <BookingDialog
          date={booking === 'blank' ? date : booking.date}
          prefill={booking === 'blank' ? null : booking}
          schedule={data}
          roster={roster.data ?? []}
          ownStaffId={ownStaffId}
          onClose={() => setBooking(null)}
        />
      )}

      {override && (
        <OverrideConfirm
          sentences={describeRules(
            override.rules,
            {
              staffId: override.appointment.staff.id,
              name: override.appointment.staff.display_name,
              startsAt: moved(override.appointment, override.change).starts_at,
              endsAt: moved(override.appointment, override.change).ends_at,
            },
            data,
          )}
          forbidden={whyNotOverride(user, ownStaffId, override.appointment.staff.id)}
          pending={change.isPending}
          onConfirm={(reason) => {
            const { appointment, change: asked, before } = override
            setOverride(null)
            change.mutate({
              appointment,
              change: { ...asked, override: true, override_reason: reason },
              before,
            })
          }}
          onCancel={() => {
            if (override.before) queryClient.setQueryData(key, override.before)
            setOverride(null)
            queryClient.invalidateQueries({ queryKey: SCHEDULE })
          }}
        />
      )}
    </div>
  )
}

type MoveVariables = { appointment: Appointment; change: Change; before?: Schedule }

/** A move the server answered `override_available`: what was asked, the rules, and the
 *  picture from before it, for Cancel. */
type PendingMove = MoveVariables & { rules: OverrideRule[] }

/** What the cancel dialog is about: one appointment, or (from "Cancel visit") its whole
 *  booking group. */
type CancelTarget = { kind: 'appointment'; appointment: Appointment } | { kind: 'group'; groupId: string }

/** Radix refuses an empty `SelectItem` value; ids are UUIDs, so nothing collides with this. */
const EVERYONE = 'everyone'

/** The grid's instants carry milliseconds, the server's do not; the moment is the same. */
const sameInstant = (a: string, b: string) => new Date(a).getTime() === new Date(b).getTime()

/** Monday to Sunday around `date`, on the string — the week the ISO calendar puts it in. */
function weekOf(date: string): [string, string] {
  const [y, m, d] = date.split('-').map(Number)
  const weekday = (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7
  const monday = addDays(date, -weekday)
  return [monday, addDays(monday, 6)]
}

/**
 * The column axis. Day view: the staff working that day, or with something on it, in roster
 * order — and everybody when nobody is, so an unconfigured week is a dimmed grid rather than
 * a blank one. Week view: the seven dates.
 */
function columnsFor(
  view: 'day' | 'week',
  schedule: Schedule,
  range: string[],
  todayDate: string,
  staffFilter: string | null,
): Column[] {
  if (view === 'week') {
    return Array.from({ length: 7 }, (_, i) => addDays(range[0], i)).map((date) => {
      const { weekday, day } = weekdayLabel(date)
      return {
        key: date,
        date,
        staffId: staffFilter,
        label: weekday,
        sublabel: String(day),
        today: date === todayDate,
        concurrency: schedule.staff.find((s) => s.id === staffFilter)?.max_concurrent_appointments,
      }
    })
  }
  const date = range[0]
  const working = new Set(schedule.working_blocks.filter((b) => b.date === date).map((b) => b.staff_id))
  for (const a of schedule.appointments) {
    if (localDate(new Date(a.starts_at), schedule.timezone) === date) working.add(a.staff.id)
  }
  const staff = working.size > 0 ? schedule.staff.filter((s) => working.has(s.id)) : schedule.staff
  return staff.map((s) => ({
    key: s.id,
    date,
    staffId: s.id,
    label: s.display_name,
    today: date === todayDate,
    concurrency: s.max_concurrent_appointments,
  }))
}

/** The appointment as it will be once the server agrees — for the card to land there now. */
function moved(a: Appointment, change: Change): Appointment {
  const starts_at = change.starts_at ?? a.starts_at
  const duration_minutes = change.duration_minutes ?? a.duration_minutes
  const ends_at = new Date(new Date(starts_at).getTime() + duration_minutes * 60_000).toISOString()
  return { ...a, starts_at, ends_at, duration_minutes }
}

/** The staff colour, in whichever of its two hexes the current theme wants. */
function ColourDot({ member }: { member: RosterEntry }) {
  const { resolvedTheme } = useTheme()
  return (
    <span
      aria-hidden
      className="size-3 shrink-0 rounded-full ring-1 ring-foreground/10"
      style={{ backgroundColor: resolvedTheme === 'dark' ? member.dark_hex : member.hex }}
    />
  )
}

const ANY = 'any'

/** A booking refused because the chosen time is gone — the lost race (`slot_taken`, the
 *  database refused) or the engine's own refusal (`not_offered`). Both mean "what you were
 *  looking at is out of date", and the screen answers both the same way. */
function stalePick(error: unknown): error is ApiError {
  return error instanceof ApiError && (error.code === 'slot_taken' || error.code === 'not_offered')
}

/**
 * Service → provider (or any) → day → a slot the server offered → the client → notes → book.
 *
 * The slots are re-read whenever the three inputs above them change, and again after a
 * stale pick — the one refusal that means "what you were looking at is out of date".
 *
 * A time drawn on the grid that the server does not offer for a named provider stays
 * bookable: the server answers `override_available` with the rules, the confirm dialog
 * asks, and Confirm books it with `override: true`. That is how a therapist willing to stay
 * past their shift end is booked from the same dialog (tech-stack §22).
 */
function BookingDialog(props: {
  date: string
  /** What was drawn on the grid: the column's person and the instant at the top of the
   *  range. The service is still to be chosen; once it is, that instant is picked if the
   *  server offers it, and said to be unavailable if not. */
  prefill: Prefill | null
  schedule: Schedule
  roster: RosterEntry[]
  ownStaffId: string | null
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const { user } = useSession()
  const [serviceId, setServiceId] = useState('')
  const [staffId, setStaffId] = useState(props.prefill?.staffId ?? ANY)
  const [date, setDate] = useState(props.date)
  const [picked, setPicked] = useState<AvailabilitySlot | null>(null)
  const [wanted, setWanted] = useState(props.prefill?.startsAt ?? null)
  const [existing, setExisting] = useState(true)
  const [search, setSearch] = useState('')
  const [customer, setCustomer] = useState<Customer | null>(null)
  const [draft, setDraft] = useState({ first_name: '', last_name: '', email: '', phone: '' })
  const [notes, setNotes] = useState('')
  const [override, setOverride] = useState<OverrideRule[] | null>(null)
  // "Add another service" (Task 18): the chain beyond the first link. Each has its own
  // service and provider (or "any"); the whole visit shares one customer and one start.
  const [links, setLinks] = useState<{ serviceId: string; staffId: string }[]>([])
  const [groupPicked, setGroupPicked] = useState<GroupSlot | null>(null)
  const chained = links.length > 0

  const catalog = useQuery({ queryKey: CATALOG, queryFn: fetchCatalog })
  const service = catalog.data?.find((s) => s.id === serviceId)
  const providers = props.roster.filter((m) => service?.staff_ids.includes(m.id))
  const availability = useQuery({
    queryKey: [...AVAILABILITY, serviceId, staffId, date],
    queryFn: () =>
      fetchAvailability({
        service_id: serviceId,
        from: date,
        to: date,
        staff_id: staffId === ANY ? undefined : staffId,
      }),
    enabled: Boolean(service?.bookable && date) && !chained,
    retry: false,
  })
  const chainServiceIds = chained ? [serviceId, ...links.map((l) => l.serviceId)] : []
  const chainStaffIds = chained
    ? [staffId, ...links.map((l) => l.staffId)].map((s) => (s === ANY ? null : s))
    : []
  const chainReady = chained && chainServiceIds.every((id) => id !== '')
  const groupAvailability = useQuery({
    queryKey: [...AVAILABILITY, 'group', ...chainServiceIds, ...chainStaffIds, date],
    queryFn: () =>
      fetchGroupAvailability({ serviceIds: chainServiceIds, staffIds: chainStaffIds, from: date, to: date }),
    enabled: chainReady && Boolean(date),
    retry: false,
  })
  const groupSlots = groupAvailability.data?.days[0]?.slots ?? []
  const slots = availability.data?.days[0]?.slots ?? []
  const timezone = (chained ? groupAvailability.data?.timezone : availability.data?.timezone) ?? props.schedule.timezone
  const provider = providers.find((m) => m.id === staffId) ?? null
  // The drawn time is the pick as soon as a service makes it an offer — and again after
  // every change of service or provider — until a slot is picked by hand.
  const offered = wanted ? (slots.find((s) => sameInstant(s.starts_at, wanted)) ?? null) : null
  const drawnButGone = Boolean(wanted && service && availability.isSuccess && !offered)
  // Drawn for one person and not offered: still sendable, as the override question. The
  // server says which rules it breaks — or that a room is busy, which nobody overrides.
  const drawn: AvailabilitySlot | null =
    drawnButGone && provider && service && wanted
      ? {
          starts_at: wanted,
          ends_at: new Date(new Date(wanted).getTime() + service.duration_minutes * 60_000).toISOString(),
          staff_ids: [provider.id],
        }
      : null
  const slot = picked ?? offered ?? drawn
  const pick = (chosen: AvailabilitySlot) => {
    setWanted(null)
    setPicked(chosen)
  }
  const matches = useQuery({
    queryKey: [...CUSTOMERS, search],
    queryFn: () => searchCustomers(search),
    enabled: existing && search.trim().length > 0,
  })

  const choose = (next: { service?: string; staff?: string; date?: string }) => {
    // Any of the three changes what is on offer; a slot picked under the old answer is not
    // an answer to the new question.
    setPicked(null)
    if (next.service !== undefined) {
      setServiceId(next.service)
      // The person stays chosen when they can deliver the new service — the one drawn on
      // the grid especially — and only falls back to "any" when they cannot.
      const eligible = catalog.data?.find((s) => s.id === next.service)?.staff_ids ?? []
      if (!eligible.includes(staffId)) setStaffId(ANY)
    }
    if (next.staff !== undefined) setStaffId(next.staff)
    if (next.date !== undefined) setDate(next.date)
  }
  // A slot picked under a person's name is a pick of that person too: the provider becomes
  // them, so the booking honours the choice rather than handing it to whoever is first in
  // column order. The slot survives, because it is in their own list as well.
  const pickUnder = (member: RosterEntry, picked: AvailabilitySlot) => {
    setStaffId(member.id)
    pick(picked)
  }

  const customerReady = existing
    ? customer !== null
    : draft.first_name.trim() !== '' && draft.last_name.trim() !== ''
  const ready = chained
    ? Boolean(service && groupPicked && chainReady && customerReady)
    : Boolean(service && slot && customerReady)

  const addLink = () => {
    setLinks([...links, { serviceId: '', staffId: ANY }])
    setGroupPicked(null)
  }
  const removeLink = (i: number) => {
    setLinks(links.filter((_, index) => index !== i))
    setGroupPicked(null)
  }
  const changeLink = (i: number, next: Partial<{ serviceId: string; staffId: string }>) => {
    setLinks(links.map((l, index) => (index === i ? { ...l, ...next } : l)))
    setGroupPicked(null)
  }

  const bookChain = useMutation({
    mutationFn: () => {
      const draftLinks: GroupLinkDraft[] = [
        { service_id: serviceId, staff_id: staffId === ANY ? null : staffId },
        ...links.map((l) => ({ service_id: l.serviceId, staff_id: l.staffId === ANY ? null : l.staffId })),
      ]
      const body = {
        starts_at: (groupPicked as GroupSlot).starts_at,
        links: draftLinks,
        notes: notes.trim() || null,
      } as Parameters<typeof bookGroup>[0]
      if (existing) body.customer_id = (customer as Customer).id
      else {
        body.customer = {
          first_name: draft.first_name.trim(),
          last_name: draft.last_name.trim(),
          email: draft.email.trim() || null,
          phone: draft.phone.trim() || null,
        }
      }
      return bookGroup(body)
    },
    onSuccess: (made) => {
      toast.success(`Booked the visit — ${made.appointments.length} appointments linked`)
      queryClient.invalidateQueries({ queryKey: SCHEDULE })
      queryClient.invalidateQueries({ queryKey: APPOINTMENTS })
      queryClient.invalidateQueries({ queryKey: AVAILABILITY })
      props.onClose()
    },
    onError: (error) => {
      if (stalePick(error)) {
        toast.error(error.message, { duration: 8000 })
        setGroupPicked(null)
        queryClient.invalidateQueries({ queryKey: AVAILABILITY })
      }
    },
  })

  const book = useMutation({
    mutationFn: (confirmed: Override) => {
      const body: BookingDraft = {
        service_id: serviceId,
        staff_id: staffId === ANY ? null : staffId,
        starts_at: (slot as AvailabilitySlot).starts_at,
        notes: notes.trim() || null,
        ...confirmed,
      }
      if (existing) body.customer_id = (customer as Customer).id
      else {
        body.customer = {
          first_name: draft.first_name.trim(),
          last_name: draft.last_name.trim(),
          email: draft.email.trim() || null,
          phone: draft.phone.trim() || null,
        }
      }
      return bookAppointment(body)
    },
    onSuccess: (made) => {
      const where = made.resources.length > 0 ? ` in ${made.resources.map((r) => r.name).join(', ')}` : ''
      toast.success(
        `Booked ${made.customer.first_name} ${made.customer.last_name} with ${made.staff.display_name}${where}`,
      )
      queryClient.invalidateQueries({ queryKey: SCHEDULE })
      queryClient.invalidateQueries({ queryKey: APPOINTMENTS })
      queryClient.invalidateQueries({ queryKey: AVAILABILITY })
      props.onClose()
    },
    onError: (error) => {
      const rules = overridableRules(error)
      if (rules) {
        setOverride(rules)
        return
      }
      if (stalePick(error)) {
        // Longer than the default: this is the one message that changes what to do next.
        toast.error(error.message, { duration: 8000 })
        setPicked(null)
        setWanted(null)
        queryClient.invalidateQueries({ queryKey: AVAILABILITY })
        queryClient.invalidateQueries({ queryKey: APPOINTMENTS })
      }
    },
  })

  const problem = chained
    ? bookChain.error && !stalePick(bookChain.error)
      ? `${bookChain.error.message}${
          refusedLinkIndex(bookChain.error) !== null ? ` (service ${refusedLinkIndex(bookChain.error)! + 1})` : ''
        }`
      : null
    : book.error && !stalePick(book.error) && !overridableRules(book.error)
      ? book.error.message
      : null
  const unbookable =
    availability.error instanceof ApiError && availability.error.code === 'not_bookable'
      ? ((availability.error.body as { unbookable_reasons?: string[] })?.unbookable_reasons ?? [])
      : service && !service.bookable
        ? service.unbookable_reasons
        : []

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>New appointment</DialogTitle>
          <DialogDescription>
            Pick the service and a time the server says is free, then who it is for.
          </DialogDescription>
        </DialogHeader>

        <Form onSubmit={() => ready && (chained ? bookChain.mutate() : book.mutate({}))}>
          <Field label="Service" htmlFor="booking-service">
            <Select value={serviceId} onValueChange={(id) => choose({ service: id })}>
              <SelectTrigger id="booking-service" aria-label="Service" className="w-full">
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

          <div className="grid grid-cols-2 gap-4">
            <Field label="Provider" htmlFor="booking-provider">
              <Select value={staffId} onValueChange={(id) => choose({ staff: id })} disabled={!service}>
                <SelectTrigger id="booking-provider" aria-label="Provider" className="w-full">
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
            <Field label="Day" htmlFor="booking-date">
              <Input
                id="booking-date"
                type="date"
                required
                value={date}
                onChange={(e) => e.target.value && choose({ date: e.target.value })}
              />
            </Field>
          </div>

          {links.map((link, i) => {
            const linkService = catalog.data?.find((s) => s.id === link.serviceId)
            const linkProviders = props.roster.filter((m) => linkService?.staff_ids.includes(m.id))
            return (
              <div key={i} className="grid grid-cols-[1fr_1fr_auto] items-end gap-2 rounded-lg border p-2">
                <Field label={`Service ${i + 2}`} htmlFor={`booking-service-${i}`}>
                  <Select
                    value={link.serviceId}
                    onValueChange={(id) => changeLink(i, { serviceId: id, staffId: ANY })}
                  >
                    <SelectTrigger id={`booking-service-${i}`} aria-label={`Service ${i + 2}`} className="w-full">
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
                <Field label="Provider" htmlFor={`booking-provider-${i}`}>
                  <Select
                    value={link.staffId}
                    onValueChange={(id) => changeLink(i, { staffId: id })}
                    disabled={!linkService}
                  >
                    <SelectTrigger id={`booking-provider-${i}`} aria-label={`Provider for service ${i + 2}`} className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={ANY}>Any available</SelectItem>
                      {linkProviders.map((m) => (
                        <SelectItem key={m.id} value={m.id}>
                          {m.display_name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </Field>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  aria-label={`Remove service ${i + 2}`}
                  onClick={() => removeLink(i)}
                >
                  Remove
                </Button>
              </div>
            )
          })}
          <Button type="button" variant="outline" size="sm" className="self-start" onClick={addLink}>
            <Plus aria-hidden />
            Add another service
          </Button>

          <fieldset className="flex flex-col gap-2">
            <legend className="mb-2 text-sm font-medium">Time</legend>
            {chained ? (
              !chainReady ? (
                <p className="text-xs text-muted-foreground">Choose every service to see times.</p>
              ) : groupAvailability.isPending ? (
                <Skeleton className="h-9 w-full" />
              ) : groupAvailability.isError ? (
                <p role="alert" className="text-xs text-destructive">
                  {groupAvailability.error.message}
                </p>
              ) : groupSlots.length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  Nothing free for the whole visit, back to back, on this day.
                </p>
              ) : (
                <div className="flex flex-wrap gap-1.5" role="group" aria-label="Visit start">
                  {groupSlots.map((s) => (
                    <Button
                      key={s.starts_at}
                      type="button"
                      size="sm"
                      variant={groupPicked?.starts_at === s.starts_at ? 'default' : 'outline'}
                      aria-pressed={groupPicked?.starts_at === s.starts_at}
                      className="flex h-auto flex-col items-start px-2.5 py-1.5"
                      onClick={() => setGroupPicked(s)}
                    >
                      <span className="tabular-nums">{clock(s.starts_at, timezone)}</span>
                      <span className="text-[10px] font-normal opacity-80">
                        {s.staff_ids
                          .map((id) => props.roster.find((m) => m.id === id)?.display_name ?? '?')
                          .join(' → ')}
                      </span>
                    </Button>
                  ))}
                </div>
              )
            ) : !service ? (
              <p className="text-xs text-muted-foreground">
                {wanted
                  ? `Drawn for ${clock(wanted, timezone)}. Choose a service to book it.`
                  : 'Choose a service first.'}
              </p>
            ) : unbookable.length > 0 ? (
              <p role="alert" className="text-xs text-destructive">
                This service cannot be booked yet: {unbookable.join(' ')}
              </p>
            ) : availability.isPending ? (
              <Skeleton className="h-9 w-full" />
            ) : availability.isError ? (
              <p role="alert" className="text-xs text-destructive">{availability.error.message}</p>
            ) : slots.length === 0 ? (
              <p className="text-xs text-muted-foreground">Nothing free on this day.</p>
            ) : staffId === ANY ? (
              // An "Any" row for a true don't-care pick — every start somebody could take,
              // once; the server assigns whoever is first in column order — then each person's
              // own starts, where a pick is a pick of them.
              <>
                <div className="flex flex-col gap-1">
                  <span className="text-xs font-medium text-muted-foreground">Any available</span>
                  <SlotButtons group="Any available" slots={slots} chosen={slot} timezone={timezone} onPick={pick} />
                </div>
                {providers.map((m) => {
                  const theirs = slots.filter((s) => s.staff_ids.includes(m.id))
                  if (theirs.length === 0) return null
                  return (
                    <div key={m.id} className="flex flex-col gap-1">
                      <span className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
                        <ColourDot member={m} />
                        {m.display_name}
                      </span>
                      <SlotButtons
                        group={m.display_name}
                        slots={theirs}
                        chosen={null}
                        timezone={timezone}
                        onPick={(picked) => pickUnder(m, picked)}
                      />
                    </div>
                  )
                })}
              </>
            ) : (
              <SlotButtons slots={slots} chosen={slot} timezone={timezone} onPick={pick} />
            )}
            {drawnButGone && (
              <p role="status" className="text-xs text-warning">
                {provider
                  ? `${clock(wanted as string, timezone)} is not offered for this service with ${provider.display_name}. Booking it anyway will ask you to confirm.`
                  : `${clock(wanted as string, timezone)} is not free for this service. Pick another time.`}
              </p>
            )}
          </fieldset>

          <fieldset className="flex flex-col gap-2">
            <legend className="mb-2 text-sm font-medium">Client</legend>
            <div className="flex gap-2">
              <Button
                type="button"
                size="sm"
                variant={existing ? 'default' : 'outline'}
                aria-pressed={existing}
                onClick={() => setExisting(true)}
              >
                Existing
              </Button>
              <Button
                type="button"
                size="sm"
                variant={existing ? 'outline' : 'default'}
                aria-pressed={!existing}
                onClick={() => setExisting(false)}
              >
                New client
              </Button>
            </div>
            {existing ? (
              customer ? (
                <div className="flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-sm">
                  <span className="flex flex-wrap items-baseline gap-x-2">
                    <span className="font-medium">
                      {customer.first_name} {customer.last_name}
                    </span>
                    {customer.phone && (
                      <span className="tabular-nums text-muted-foreground">{formatPhone(customer.phone)}</span>
                    )}
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
                    <p className="text-xs text-muted-foreground">Nobody matches. Add them as a new client.</p>
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
              <div className="grid grid-cols-2 gap-4">
                <Field label="First name" htmlFor="booking-first-name">
                  <Input
                    id="booking-first-name"
                    required
                    maxLength={100}
                    value={draft.first_name}
                    onChange={(e) => setDraft({ ...draft, first_name: e.target.value })}
                  />
                </Field>
                <Field label="Last name" htmlFor="booking-last-name">
                  <Input
                    id="booking-last-name"
                    required
                    maxLength={100}
                    value={draft.last_name}
                    onChange={(e) => setDraft({ ...draft, last_name: e.target.value })}
                  />
                </Field>
                <Field label="Email" htmlFor="booking-email">
                  <Input
                    id="booking-email"
                    type="email"
                    value={draft.email}
                    onChange={(e) => setDraft({ ...draft, email: e.target.value })}
                  />
                </Field>
                <Field label="Phone" htmlFor="booking-phone">
                  <Input
                    id="booking-phone"
                    type="tel"
                    value={draft.phone}
                    onChange={(e) => setDraft({ ...draft, phone: e.target.value })}
                  />
                </Field>
              </div>
            )}
          </fieldset>

          <Field label="Notes" htmlFor="booking-notes">
            <Textarea
              id="booking-notes"
              rows={2}
              maxLength={2000}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
            />
          </Field>

          {problem && <FormError>{problem}</FormError>}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={props.onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={!ready || book.isPending || bookChain.isPending}>
              Book
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>

      {override && provider && slot && (
        <OverrideConfirm
          sentences={describeRules(
            override,
            { staffId: provider.id, name: provider.display_name, startsAt: slot.starts_at, endsAt: slot.ends_at },
            props.schedule,
          )}
          forbidden={whyNotOverride(user, props.ownStaffId, provider.id)}
          pending={book.isPending}
          onConfirm={(reason) => {
            setOverride(null)
            book.mutate({ override: true, override_reason: reason })
          }}
          onCancel={() => {
            // Back to the slot picker: the time that needed confirming is no longer the pick.
            setOverride(null)
            setPicked(null)
            setWanted(null)
          }}
        />
      )}
    </Dialog>
  )
}

function SlotButtons(props: {
  slots: AvailabilitySlot[]
  chosen: AvailabilitySlot | null
  timezone?: string
  /** Named when several lists share a dialog, so "10:00 AM under Ana" is its own control. */
  group?: string
  onPick: (slot: AvailabilitySlot) => void
}) {
  return (
    <div className="flex flex-wrap gap-1.5" role="group" aria-label={props.group}>
      {props.slots.map((s) => (
        <Button
          key={s.starts_at}
          type="button"
          size="sm"
          variant={props.chosen?.starts_at === s.starts_at ? 'default' : 'outline'}
          aria-pressed={props.chosen?.starts_at === s.starts_at}
          className="tabular-nums"
          onClick={() => props.onPick(s)}
        >
          {clock(s.starts_at, props.timezone)}
        </Button>
      ))}
    </div>
  )
}

