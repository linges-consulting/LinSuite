import { useMutation, useQuery } from '@tanstack/react-query'
import { CalendarDays, ChevronLeft, ChevronRight, CircleCheck, Copy } from 'lucide-react'
import { forwardRef, useRef, useState } from 'react'
import { toast } from 'sonner'
import { SlotButtons } from '@/components/calendar/slot-buttons'
import { EmptyState } from '@/components/empty-state'
import { Field, Form, FormError } from '@/components/form'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import {
  ApiError,
  createPublicBooking,
  fetchPublicAvailability,
  fetchPublicServices,
  type AvailabilitySlot,
  type PublicBooking,
  type PublicService,
} from '@/lib/api'
import { clock, today } from '@/lib/calendar/format'
import { addDays } from '@/lib/calendar/pixels'
import { useEmbedMode, useEmbedResize } from '@/lib/embed'

const ANY = 'any'

/**
 * `/book` (`?embed=1` for the iframe variant) — the client's own booking page (Phase 6 Task
 * 6, #10): service, then provider (or "any available"), then a slot; then who is booking;
 * then the confirmation and the management link, the only recovery path when no notification
 * channel is configured (Task 3's own reasoning) — shown plainly here, with a copy button,
 * before the visitor can navigate away from it.
 *
 * Mounted in `App.tsx` outside every gate, the same way `/f/*` (`public-form.tsx`) is — no
 * session, no app-shell nav to hide. `?embed=1` (`lib/embed.ts`) only hides this page's own
 * "powered by" line, per Task 5's decision that there was never a nav here to strip.
 *
 * **One flat slot list per (service, provider, day)**, not the staff booking dialog's "any
 * available" + per-provider breakdown (`routes/schedule.tsx`'s `BookingDialog`): here the
 * provider is chosen *before* slots are shown (m3.md's own step order), so there is only ever
 * one list to render per choice. `components/calendar/slot-buttons.tsx` — extracted from
 * that dialog for this page — needed no change to serve both call sites; it was already pure
 * (`slots`/`chosen`/`timezone`/`onPick`, no calendar-only state).
 */
export function BookingPage() {
  const embed = useEmbedMode()
  const rootRef = useRef<HTMLDivElement>(null)
  useEmbedResize(rootRef, embed)

  const [step, setStep] = useState<'pick' | 'identity' | 'confirmation'>('pick')
  const [serviceId, setServiceId] = useState('')
  const [staffId, setStaffId] = useState(ANY)
  // The business's own timezone isn't known until the first availability response — before
  // that, the browser's own guess is the only date to start from.
  const [guessZone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone)
  const [date, setDate] = useState(() => today(guessZone))
  const [picked, setPicked] = useState<AvailabilitySlot | null>(null)
  const [booking, setBooking] = useState<PublicBooking | null>(null)

  const services = useQuery({ queryKey: ['public-services'], queryFn: fetchPublicServices })
  const service = services.data?.services.find((s) => s.id === serviceId) ?? null

  const availability = useQuery({
    queryKey: ['public-availability', serviceId, staffId, date],
    queryFn: () =>
      fetchPublicAvailability({
        service_id: serviceId,
        from: date,
        to: date,
        staff_id: staffId === ANY ? undefined : staffId,
      }),
    enabled: Boolean(serviceId && date),
    retry: false,
  })
  const slots = availability.data?.days[0]?.slots ?? []

  const chooseService = (id: string) => {
    setServiceId(id)
    setStaffId(ANY)
    setPicked(null)
  }
  const chooseStaff = (id: string) => {
    setStaffId(id)
    setPicked(null)
  }
  const changeDay = (delta: number) => {
    setDate((d) => addDays(d, delta))
    setPicked(null)
  }

  if (services.isPending) {
    return (
      <Page ref={rootRef} embed={embed}>
        <Skeleton className="h-8 w-2/3" />
        <Skeleton className="h-48 w-full" />
      </Page>
    )
  }
  if (
    services.isError ||
    !services.data.online_booking_enabled ||
    services.data.services.length === 0
  ) {
    return (
      <Page ref={rootRef} embed={embed}>
        <EmptyState
          icon={CalendarDays}
          title="Booking isn't available right now"
          description="Please contact us directly to book an appointment."
        />
      </Page>
    )
  }

  if (step === 'confirmation' && booking) {
    return (
      <Page ref={rootRef} embed={embed}>
        <Confirmation booking={booking} />
      </Page>
    )
  }
  if (step === 'identity' && service && picked) {
    return (
      <Page ref={rootRef} embed={embed}>
        <Identity
          service={service}
          staffId={staffId === ANY ? null : staffId}
          slot={picked}
          onBack={() => setStep('pick')}
          onBooked={(made) => {
            setBooking(made)
            setStep('confirmation')
          }}
          onStale={() => {
            setPicked(null)
            setStep('pick')
            void availability.refetch()
          }}
        />
      </Page>
    )
  }

  return (
    <Page ref={rootRef} embed={embed}>
      <header>
        <h1 className="text-xl font-semibold">Book an appointment</h1>
      </header>
      <div className="flex flex-col gap-4">
        <Field label="Service" htmlFor="book-service">
          <Select value={serviceId} onValueChange={chooseService}>
            <SelectTrigger id="book-service" className="w-full">
              <SelectValue placeholder="Choose a service" />
            </SelectTrigger>
            <SelectContent>
              {services.data.services.map((s) => (
                <SelectItem key={s.id} value={s.id}>
                  {s.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        {service && (
          <Field label="Provider" htmlFor="book-staff">
            <Select value={staffId} onValueChange={chooseStaff}>
              <SelectTrigger id="book-staff" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ANY}>Any available</SelectItem>
                {service.staff.map((m) => (
                  <SelectItem key={m.id} value={m.id}>
                    {m.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
        )}
        {service && (
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium">{dayLabel(date)}</span>
              <div className="flex gap-1">
                <Button
                  type="button"
                  variant="outline"
                  size="icon"
                  aria-label="Previous day"
                  onClick={() => changeDay(-1)}
                >
                  <ChevronLeft aria-hidden />
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="icon"
                  aria-label="Next day"
                  onClick={() => changeDay(1)}
                >
                  <ChevronRight aria-hidden />
                </Button>
              </div>
            </div>
            {availability.isPending ? (
              <Skeleton className="h-9 w-full" />
            ) : availability.isError ? (
              <FormError>{availability.error.message}</FormError>
            ) : slots.length === 0 ? (
              <p className="text-sm text-muted-foreground">Nothing free on this day.</p>
            ) : (
              <SlotButtons
                slots={slots}
                chosen={picked}
                timezone={availability.data?.timezone}
                onPick={setPicked}
              />
            )}
          </div>
        )}
        <Button
          type="button"
          className="self-start"
          disabled={!picked}
          onClick={() => setStep('identity')}
        >
          Continue
        </Button>
      </div>
    </Page>
  )
}

/** `Mon, Jan 15` — built from the string, no zone, the same technique
 *  `lib/calendar/format.ts::weekdayLabel` uses to avoid a browser-local off-by-one. */
function dayLabel(date: string): string {
  const [y, m, d] = date.split('-').map(Number)
  return new Intl.DateTimeFormat('en', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(Date.UTC(y, m - 1, d)))
}

function stale(error: unknown): boolean {
  return error instanceof ApiError && (error.code === 'slot_taken' || error.code === 'not_offered')
}

function Identity(props: {
  service: PublicService
  staffId: string | null
  slot: AvailabilitySlot
  onBack: () => void
  onBooked: (booking: PublicBooking) => void
  onStale: () => void
}) {
  const [firstName, setFirstName] = useState('')
  const [lastName, setLastName] = useState('')
  const [email, setEmail] = useState('')
  const [phone, setPhone] = useState('')
  // Honeypot (#10's own acceptance criteria): no real visitor sees or fills this in — see the
  // markup below for how it stays off-screen without `display: none`.
  const [website, setWebsite] = useState('')

  const book = useMutation({
    mutationFn: () =>
      createPublicBooking({
        service_id: props.service.id,
        staff_id: props.staffId,
        starts_at: props.slot.starts_at,
        customer: {
          first_name: firstName.trim(),
          last_name: lastName.trim(),
          email: email.trim() || null,
          phone: phone.trim() || null,
        },
        website,
      }),
    onSuccess: props.onBooked,
    onError: (error) => {
      if (stale(error)) {
        toast.error('That time was just taken. Pick another.')
        props.onStale()
      }
    },
  })

  const ready =
    firstName.trim() !== '' && lastName.trim() !== '' && (email.trim() !== '' || phone.trim() !== '')
  const genericError = book.isError && !stale(book.error)

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-xl font-semibold">Your details</h1>
        <p className="text-sm text-muted-foreground">
          {props.service.name} · {clock(props.slot.starts_at)}
        </p>
      </header>
      <Form onSubmit={() => ready && !book.isPending && book.mutate()}>
        <div className="grid grid-cols-2 gap-4">
          <Field label="First name" htmlFor="book-first-name">
            <Input
              id="book-first-name"
              required
              maxLength={100}
              value={firstName}
              onChange={(e) => setFirstName(e.target.value)}
            />
          </Field>
          <Field label="Last name" htmlFor="book-last-name">
            <Input
              id="book-last-name"
              required
              maxLength={100}
              value={lastName}
              onChange={(e) => setLastName(e.target.value)}
            />
          </Field>
          <Field label="Email" htmlFor="book-email" hint="Email or phone — at least one.">
            <Input
              id="book-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </Field>
          <Field label="Phone" htmlFor="book-phone">
            <Input id="book-phone" type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} />
          </Field>
        </div>
        {/* Honeypot: off-screen rather than `display: none`/`visibility: hidden`, which a
            bot's naive form-filler may skip because it checks computed style before it is
            filled at all — this stays laid out, just moved off the visible page. `aria-hidden`
            plus `tabIndex={-1}` keep a sighted or assistive-tech visitor from ever tabbing
            into or hearing about it, so nobody real can stumble into filling it in. */}
        <div
          aria-hidden="true"
          style={{ position: 'absolute', left: '-9999px', top: 0, width: 1, height: 1, overflow: 'hidden' }}
        >
          <label htmlFor="book-website">Leave this field blank</label>
          <input
            id="book-website"
            name="website"
            type="text"
            tabIndex={-1}
            autoComplete="off"
            value={website}
            onChange={(e) => setWebsite(e.target.value)}
          />
        </div>
        {genericError && <FormError>{book.error.message}</FormError>}
        <div className="flex gap-2">
          <Button type="button" variant="outline" onClick={props.onBack}>
            Back
          </Button>
          <Button type="submit" disabled={!ready || book.isPending}>
            Confirm booking
          </Button>
        </div>
      </Form>
    </div>
  )
}

function Confirmation({ booking }: { booking: PublicBooking }) {
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(booking.management_link)
      toast.success('Link copied')
    } catch {
      toast.error('Could not copy. Select the link and copy it instead.')
    }
  }
  return (
    <div className="flex flex-col items-center gap-4 rounded-xl border px-6 py-10 text-center">
      <CircleCheck className="size-6 text-primary" aria-hidden />
      <h1 className="text-lg font-semibold">You're booked</h1>
      <p>
        {booking.service_name} with {booking.staff_name}
      </p>
      <p className="font-medium">
        {new Intl.DateTimeFormat(undefined, { dateStyle: 'full' }).format(new Date(booking.starts_at))}
        {' · '}
        {clock(booking.starts_at)}
      </p>
      <div className="flex w-full max-w-md flex-col gap-2 text-left">
        <Label htmlFor="management-link">Manage this booking</Label>
        <div className="flex gap-2">
          <Input
            id="management-link"
            readOnly
            value={booking.management_link}
            onFocus={(e) => e.target.select()}
          />
          <Button type="button" variant="outline" onClick={copy}>
            <Copy aria-hidden />
            Copy
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          Save this link — it's the only way to change or cancel this booking online.
        </p>
      </div>
    </div>
  )
}

const Page = forwardRef<HTMLDivElement, { embed: boolean; children: React.ReactNode }>(
  function Page({ embed, children }, ref) {
    return (
      <main
        ref={ref}
        // No `min-h-dvh` in embed mode: `useEmbedResize` sizes the iframe to this element's
        // own content height, and forcing full-viewport height here would defeat that — the
        // iframe would always read as viewport-tall no matter how short the step on screen is.
        className={`mx-auto flex w-full max-w-2xl flex-col gap-6 px-4 py-8 ${embed ? '' : 'min-h-dvh'}`}
      >
        {children}
        {!embed && (
          <p className="mt-auto pt-6 text-center text-xs text-muted-foreground">Powered by LinSuite</p>
        )}
      </main>
    )
  },
)
