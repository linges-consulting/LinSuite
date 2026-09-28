import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { forwardRef, useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router'
import { toast } from 'sonner'
import { SlotButtons } from '@/components/calendar/slot-buttons'
import { DeadEnd } from '@/components/dead-end'
import { FormError } from '@/components/form'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import {
  ApiError,
  cancelManageBooking,
  fetchManageBooking,
  fetchPublicAvailability,
  rescheduleManageBooking,
  type AvailabilitySlot,
  type ManageBooking,
} from '@/lib/api'
import { clock, today } from '@/lib/calendar/format'
import { addDays } from '@/lib/calendar/pixels'
import { useEmbedMode, useEmbedResize } from '@/lib/embed'

const MANAGE_KEY = ['manage-booking']

/**
 * `/manage-booking/#<token>` — the client's own way back into a booking made through `/book`
 * (Phase 6 Task 3/6, #10): view it, and if the business still allows it, cancel or reschedule.
 *
 * The token is the URL's **fragment** — `scheduling/public.py::_management_url` mints links
 * shaped exactly this way, never a path segment, the same reason `/f/#<token>` keeps a form
 * link's token out of a server log (`routes/public-form.tsx`). Read once, on mount, then
 * scrubbed from the address bar; a signed-in browser sees exactly what the client sees, and
 * every request here omits credentials.
 *
 * **One 404 for every dead-link reason** (`code: 'link_invalid'`): unknown token, already
 * cancelled, completed, no-showed, or simply past its start — `DeadEnd` reads the same either
 * way, mirroring `/f/*`'s own discipline rather than inventing a second dead-link screen.
 * **A different shape entirely** for "the link is fine, but the business's cutoff/toggle says
 * no right now" (`code: 'online_change_not_allowed'`, 422) — that is not a dead link, so it
 * stays on the booking's own view with a clear inline message instead.
 */
export function ManageBookingPage() {
  const embed = useEmbedMode()
  const rootRef = useRef<HTMLDivElement>(null)
  useEmbedResize(rootRef, embed)

  const { hash } = useLocation()
  const [token] = useState(() => hash.replace(/^#/, ''))
  useEffect(() => {
    window.history.replaceState(null, '', '/manage-booking/')
  }, [])

  const queryClient = useQueryClient()
  const [mode, setMode] = useState<'view' | 'confirm-cancel' | 'reschedule'>('view')

  const manage = useQuery({
    queryKey: [...MANAGE_KEY, token],
    queryFn: () => fetchManageBooking(token),
    enabled: token !== '',
    retry: false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  })

  const cancel = useMutation({
    mutationFn: () => cancelManageBooking(token),
    onSuccess: (updated) => {
      queryClient.setQueryData([...MANAGE_KEY, token], updated)
      setMode('view')
      toast.success('Booking cancelled')
    },
    onError: (error) => {
      // The link itself went dead between opening the page and clicking Cancel (somebody
      // else cancelled it first, or its start just passed) — fall into the same dead-end
      // view a stale open of the link would have shown, rather than a bare error banner.
      if (error instanceof ApiError && error.status === 404) void manage.refetch()
    },
  })

  if (token === '') {
    return (
      <Page ref={rootRef} embed={embed}>
        <DeadEnd title="This link is no longer valid" text="Please contact us directly." />
      </Page>
    )
  }
  if (manage.isPending) {
    return (
      <Page ref={rootRef} embed={embed}>
        <Skeleton className="h-8 w-2/3" />
        <Skeleton className="h-32 w-full" />
      </Page>
    )
  }
  if (manage.isError) {
    return (
      <Page ref={rootRef} embed={embed}>
        <DeadEnd title="This link is no longer valid" text="Please contact us directly." />
      </Page>
    )
  }

  const booking = manage.data
  if (mode === 'reschedule') {
    return (
      <Page ref={rootRef} embed={embed}>
        <Reschedule
          booking={booking}
          token={token}
          onDone={(updated) => {
            queryClient.setQueryData([...MANAGE_KEY, token], updated)
            setMode('view')
            toast.success('Booking rescheduled')
          }}
          onCancel={() => setMode('view')}
        />
      </Page>
    )
  }

  return (
    <Page ref={rootRef} embed={embed}>
      <header>
        <h1 className="text-xl font-semibold">Your booking</h1>
      </header>
      <div className="flex flex-col gap-2 rounded-xl border px-6 py-6">
        <p className="font-medium">
          {booking.service_name} with {booking.staff_name}
        </p>
        <p>
          {new Intl.DateTimeFormat(undefined, { dateStyle: 'full' }).format(new Date(booking.starts_at))}
          {' · '}
          {clock(booking.starts_at)}
        </p>
        {booking.status !== 'confirmed' && (
          <p className="text-sm text-muted-foreground">
            This booking is {booking.status.replace('_', ' ')}.
          </p>
        )}
      </div>
      {booking.status === 'confirmed' && !booking.cancellable && (
        <p className="text-sm text-muted-foreground">
          Online changes are no longer available for this booking. Please contact us directly.
        </p>
      )}
      {booking.status === 'confirmed' &&
        booking.cancellable &&
        (mode === 'confirm-cancel' ? (
          <div className="flex flex-col gap-2">
            <p className="text-sm">Cancel this booking? This cannot be undone.</p>
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => setMode('view')}>
                No, keep it
              </Button>
              <Button variant="destructive" disabled={cancel.isPending} onClick={() => cancel.mutate()}>
                Yes, cancel
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => setMode('reschedule')}>
              Reschedule
            </Button>
            <Button variant="destructive" onClick={() => setMode('confirm-cancel')}>
              Cancel booking
            </Button>
          </div>
        ))}
      {cancel.isError && !(cancel.error instanceof ApiError && cancel.error.status === 404) && (
        <FormError>{cancel.error.message}</FormError>
      )}
    </Page>
  )
}

function stale(error: unknown): boolean {
  return error instanceof ApiError && (error.code === 'slot_taken' || error.code === 'not_offered')
}

function Reschedule(props: {
  booking: ManageBooking
  token: string
  onDone: (booking: ManageBooking) => void
  onCancel: () => void
}) {
  const [guessZone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone)
  const [date, setDate] = useState(() => today(guessZone))
  const [picked, setPicked] = useState<AvailabilitySlot | null>(null)

  const availability = useQuery({
    queryKey: ['public-availability', props.booking.service_id, props.booking.staff_id, date],
    queryFn: () =>
      fetchPublicAvailability({
        service_id: props.booking.service_id,
        from: date,
        to: date,
        staff_id: props.booking.staff_id,
      }),
    retry: false,
  })
  const slots = availability.data?.days[0]?.slots ?? []

  const move = useMutation({
    mutationFn: () => rescheduleManageBooking(props.token, (picked as AvailabilitySlot).starts_at),
    onSuccess: props.onDone,
    onError: (error) => {
      if (stale(error)) {
        toast.error('That time was just taken. Pick another.')
        setPicked(null)
        void availability.refetch()
      }
    },
  })
  const genericError = move.isError && !stale(move.error)

  const changeDay = (delta: number) => {
    setDate((d) => addDays(d, delta))
    setPicked(null)
  }

  return (
    <div className="flex flex-col gap-4">
      <header>
        <h1 className="text-xl font-semibold">Pick a new time</h1>
        <p className="text-sm text-muted-foreground">
          {props.booking.service_name} with {props.booking.staff_name}
        </p>
      </header>
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
      {genericError && <FormError>{move.error.message}</FormError>}
      <div className="flex gap-2">
        <Button type="button" variant="outline" onClick={props.onCancel}>
          Back
        </Button>
        <Button type="button" disabled={!picked || move.isPending} onClick={() => move.mutate()}>
          Confirm new time
        </Button>
      </div>
    </div>
  )
}

/** `Mon, Jan 15` — same technique `routes/booking.tsx::dayLabel` and
 *  `lib/calendar/format.ts::weekdayLabel` use, built from the string so no zone is involved. */
function dayLabel(date: string): string {
  const [y, m, d] = date.split('-').map(Number)
  return new Intl.DateTimeFormat('en', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(Date.UTC(y, m - 1, d)))
}

const Page = forwardRef<HTMLDivElement, { embed: boolean; children: React.ReactNode }>(
  function Page({ embed, children }, ref) {
    return (
      <main
        ref={ref}
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
