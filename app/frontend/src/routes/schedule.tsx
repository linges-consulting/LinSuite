import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CalendarDays, ChevronLeft, ChevronRight, Plus } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
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
import { Textarea } from '@/components/ui/textarea'
import {
  ApiError,
  bookAppointment,
  fetchAppointments,
  fetchAvailability,
  fetchCatalog,
  fetchRoster,
  searchCustomers,
  type Appointment,
  type AvailabilitySlot,
  type BookingDraft,
  type Customer,
  type RosterEntry,
} from '@/lib/api'
import { useBranding } from '@/lib/branding'
import { formatPhone } from '@/lib/phone'
import { APPOINTMENTS, AVAILABILITY, CATALOG, CUSTOMERS, ROSTER } from '@/lib/query-keys'
import { useTheme } from '@/lib/theme'
import { cn } from '@/lib/utils'

/**
 * The schedule: one column per staff member, one day at a time, and the way to put an
 * appointment on it (PRD §3). Lists rather than a time grid — the grid is Task 16; this is
 * the tracer bullet that proves a booking goes in and comes back out.
 *
 * **The slots come from the server, and so does the decision.** The dialog never computes a
 * time: it shows what `/api/availability` offered and sends back the instant it was given.
 * Two people can still pick the same slot in the same minute; the database refuses the second
 * and the screen says so and refreshes, because that is the honest answer.
 *
 * **"Today" is the business's today.** The zone comes from the branding document the shell
 * has already read, so the first request this screen makes is for the right day — a
 * receptionist checking from home at 23:30 in another zone sees the day the clinic is in.
 * Times are printed in that zone too.
 */
export function SchedulePage() {
  const branding = useBranding()
  const zone = branding.data?.timezone
  const [chosen, setChosen] = useState<string | null>(null)
  const [booking, setBooking] = useState(false)
  const roster = useQuery({ queryKey: ROSTER, queryFn: fetchRoster })
  // Until the zone is known there is no "today" to ask for.
  const date = chosen ?? (zone ? today(zone) : null)
  const day = useQuery({
    queryKey: [...APPOINTMENTS, date],
    queryFn: () => fetchAppointments({ from: date as string, to: date as string }),
    enabled: date !== null,
    // Stepping a day is a different query; keep the columns up rather than flashing a skeleton.
    placeholderData: (previous) => previous,
  })

  if (!date || roster.isPending || day.isPending) return <Skeleton className="h-64 w-full" />
  const failed = [branding, roster, day].find((q) => q.isError)
  if (failed) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {failed.error?.message}
      </p>
    )
  }

  const shift = (days: number) => setChosen(addDays(date, days))
  const columns = roster.data ?? []
  const appointments = day.data?.appointments ?? []
  const timezone = day.data?.timezone ?? zone

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-2">
          <Button variant="outline" size="icon-sm" aria-label="Previous day" onClick={() => shift(-1)}>
            <ChevronLeft />
          </Button>
          <Input
            type="date"
            aria-label="Day"
            className="w-40"
            value={date}
            onChange={(e) => e.target.value && setChosen(e.target.value)}
          />
          <Button variant="outline" size="icon-sm" aria-label="Next day" onClick={() => shift(1)}>
            <ChevronRight />
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setChosen(null)}>
            Today
          </Button>
        </div>
        <Button onClick={() => setBooking(true)} disabled={columns.length === 0}>
          <Plus aria-hidden />
          New appointment
        </Button>
      </div>

      {columns.length === 0 ? (
        <EmptyState
          icon={CalendarDays}
          title="Nobody on the schedule yet"
          description="Staff members appear here as columns once they are added in Settings."
        />
      ) : (
        // The columns scroll sideways inside this box; the page itself never does (DESIGN.md).
        <div className="overflow-x-auto" data-testid="columns">
          <div
            className="grid gap-4"
            style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(14rem, 1fr))` }}
          >
            {columns.map((member) => (
              <StaffColumn
                key={member.id}
                member={member}
                timezone={timezone}
                appointments={appointments.filter((a) => a.staff.id === member.id)}
              />
            ))}
          </div>
        </div>
      )}

      {booking && (
        <BookingDialog
          date={date}
          roster={columns}
          onClose={() => setBooking(false)}
        />
      )}
    </div>
  )
}

function StaffColumn(props: { member: RosterEntry; timezone?: string; appointments: Appointment[] }) {
  return (
    <section aria-label={props.member.display_name} className="flex flex-col gap-2 rounded-xl border p-3">
      <h2 className="flex items-center gap-2 text-base font-medium">
        <ColourDot member={props.member} />
        {props.member.display_name}
      </h2>
      {props.appointments.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing booked.</p>
      ) : (
        <ol className="flex flex-col gap-2">
          {props.appointments.map((a) => (
            <li
              key={a.id}
              className={cn('rounded-lg border p-2 text-sm', a.status === 'cancelled' && 'opacity-60')}
            >
              <time className="block text-xs font-medium text-muted-foreground" dateTime={a.starts_at}>
                {clock(a.starts_at, props.timezone)}–{clock(a.ends_at, props.timezone)}
              </time>
              <span className="block font-medium">
                {a.customer.first_name} {a.customer.last_name}
              </span>
              <span className="block text-muted-foreground">
                {a.service.name}
                {a.resources.length > 0 && ` · ${a.resources.map((r) => r.name).join(', ')}`}
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
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

/** Radix refuses an empty `SelectItem` value; ids are UUIDs, so nothing collides with this. */
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
function BookingDialog(props: { date: string; roster: RosterEntry[]; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [serviceId, setServiceId] = useState('')
  const [staffId, setStaffId] = useState(ANY)
  const [date, setDate] = useState(props.date)
  const [slot, setSlot] = useState<AvailabilitySlot | null>(null)
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
  const timezone = availability.data?.timezone
  const matches = useQuery({
    queryKey: [...CUSTOMERS, search],
    queryFn: () => searchCustomers(search),
    enabled: existing && search.trim().length > 0,
  })

  const choose = (next: { service?: string; staff?: string; date?: string }) => {
    // Any of the three changes what is on offer; a slot picked under the old answer is not
    // an answer to the new question.
    setSlot(null)
    if (next.service !== undefined) {
      setServiceId(next.service)
      setStaffId(ANY)
    }
    if (next.staff !== undefined) setStaffId(next.staff)
    if (next.date !== undefined) setDate(next.date)
  }
  // A slot picked under a person's name is a pick of that person too: the provider becomes
  // them, so the booking honours the choice rather than handing it to whoever is first in
  // column order. The slot survives, because it is in their own list as well.
  const pickUnder = (member: RosterEntry, picked: AvailabilitySlot) => {
    setStaffId(member.id)
    setSlot(picked)
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
      queryClient.invalidateQueries({ queryKey: APPOINTMENTS })
      queryClient.invalidateQueries({ queryKey: AVAILABILITY })
      props.onClose()
    },
    onError: (error) => {
      if (stalePick(error)) {
        // Longer than the default: this is the one message that changes what to do next.
        toast.error(error.message, { duration: 8000 })
        setSlot(null)
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
              <p className="text-xs text-muted-foreground">Choose a service first.</p>
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
                  <SlotButtons group="Any available" slots={slots} chosen={slot} timezone={timezone} onPick={setSlot} />
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
              <SlotButtons slots={slots} chosen={slot} timezone={timezone} onPick={setSlot} />
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

// --- dates, clocks and numbers ---------------------------------------------------------------

/** Today as `YYYY-MM-DD` on the business's calendar, whatever the browser's clock says. The
 *  `en-CA` locale is the one whose default date format *is* ISO. */
function today(timezone: string): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date())
}

/** Calendar arithmetic on the string itself — no zone, no DST, no browser clock. */
function addDays(iso: string, days: number): string {
  const [y, m, d] = iso.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10)
}

/** An instant as a wall-clock time in the business's zone (or the browser's, before the
 *  zone is known). */
function clock(instant: string, timezone?: string): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: 'numeric',
    minute: '2-digit',
    timeZone: timezone,
  }).format(new Date(instant))
}
