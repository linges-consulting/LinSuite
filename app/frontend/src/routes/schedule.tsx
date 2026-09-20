import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarDays, ChevronLeft, ChevronRight, Plus } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { Grid } from '@/components/calendar/grid'
import type { Change, Column, Prefill } from '@/components/calendar/types'
import { EmptyState } from '@/components/empty-state'
import { Field, Form, FormError } from '@/components/form'
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
  changeAppointment,
  fetchAvailability,
  fetchCatalog,
  fetchRoster,
  fetchSchedule,
  searchCustomers,
  type Appointment,
  type AvailabilitySlot,
  type BookingDraft,
  type Customer,
  type RosterEntry,
  type Schedule,
} from '@/lib/api'
import { useSession } from '@/lib/auth'
import { useBranding } from '@/lib/branding'
import { clock, today, weekdayLabel } from '@/lib/calendar/format'
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
  const roster = useQuery({ queryKey: ROSTER, queryFn: fetchRoster })
  // Until the zone is known there is no "today" to ask for.
  const date = chosen ?? (zone ? today(zone) : null)
  const range = date ? (view === 'day' ? [date, date] : weekOf(date)) : null
  const filter = view === 'week' && staffFilter !== EVERYONE ? staffFilter : undefined
  const key = range ? [...SCHEDULE, range[0], range[1], filter ?? null] : SCHEDULE
  const schedule = useQuery({
    queryKey: key,
    queryFn: () => fetchSchedule({ from: range![0], to: range![1], staff_id: filter }),
    enabled: range !== null,
    // Stepping a day is a different query; keep the grid up rather than flashing a skeleton.
    placeholderData: (previous) => previous,
  })

  const change = useMutation({
    mutationFn: ({ appointment, change }: { appointment: Appointment; change: Change }) =>
      changeAppointment(appointment.id, change),
    onMutate: async ({ appointment, change }) => {
      await queryClient.cancelQueries({ queryKey: key })
      const before = queryClient.getQueryData<Schedule>(key)
      queryClient.setQueryData<Schedule>(key, (current) =>
        current && {
          ...current,
          appointments: current.appointments.map((a) =>
            a.id === appointment.id ? moved(a, change) : a,
          ),
        },
      )
      return { before }
    },
    onError: (error, _variables, context) => {
      if (context?.before) queryClient.setQueryData(key, context.before)
      toast.error(`Not moved: ${error.message}`, { duration: 8000 })
    },
    onSettled: () => {
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
        />
      )}

      {booking && (
        <BookingDialog
          date={booking === 'blank' ? date : booking.date}
          prefill={booking === 'blank' ? null : booking}
          timezone={data.timezone}
          roster={roster.data ?? []}
          onClose={() => setBooking(null)}
        />
      )}
    </div>
  )
}

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
 */
function BookingDialog(props: {
  date: string
  /** What was drawn on the grid: the column's person and the instant at the top of the
   *  range. The service is still to be chosen; once it is, that instant is picked if the
   *  server offers it, and said to be unavailable if not. */
  prefill: Prefill | null
  timezone: string
  roster: RosterEntry[]
  onClose: () => void
}) {
  const queryClient = useQueryClient()
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
    enabled: Boolean(service?.bookable && date),
    retry: false,
  })
  const slots = availability.data?.days[0]?.slots ?? []
  const timezone = availability.data?.timezone ?? props.timezone
  // The drawn time is the pick as soon as a service makes it an offer — and again after
  // every change of service or provider — until a slot is picked by hand.
  const offered = wanted ? (slots.find((s) => sameInstant(s.starts_at, wanted)) ?? null) : null
  const slot = picked ?? offered
  const drawnButGone = Boolean(wanted && service && availability.isSuccess && !offered)
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
  const ready = Boolean(service && slot && customerReady)

  const book = useMutation({
    mutationFn: () => {
      const body: BookingDraft = {
        service_id: serviceId,
        staff_id: staffId === ANY ? null : staffId,
        starts_at: (slot as AvailabilitySlot).starts_at,
        notes: notes.trim() || null,
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

  const problem = book.error && !stalePick(book.error) ? book.error.message : null
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

        <Form onSubmit={() => ready && book.mutate()}>
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

          <fieldset className="flex flex-col gap-2">
            <legend className="mb-2 text-sm font-medium">Time</legend>
            {!service ? (
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
                {clock(wanted as string, timezone)} is not free for this service
                {staffId === ANY ? '' : ` with ${providers.find((m) => m.id === staffId)?.display_name ?? 'them'}`}
                . Pick another time.
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
            <Button type="submit" disabled={!ready || book.isPending}>
              Book
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
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

