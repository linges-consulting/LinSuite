import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { ArrowLeft, ChevronLeft, ChevronRight, SearchX, UserRound, Users } from 'lucide-react'
import { useDeferredValue, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { EmptyState } from '@/components/empty-state'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import {
  ApiError,
  fetchCustomerProfile,
  fetchCustomers,
  type CustomerRecord,
  type Visit,
} from '@/lib/api'
import { formatPhone } from '@/lib/phone'
import { CUSTOMERS } from '@/lib/query-keys'

const PAGE_SIZE = 50

/**
 * Every client, alphabetically, a page at a time. The search box is the front-desk lookup:
 * a name, an email or the digits of the number on the caller ID — formatted or not — and
 * the list narrows as it is typed. The narrowing is the server's (`q`), not a filter over
 * what happens to be loaded, so it finds people on every page. The list itself is never an
 * access event (ADR-0002 §4); opening a row is.
 */
export function ClientsPage() {
  const navigate = useNavigate()
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  // Keystrokes update the box at once; the query follows when React has a moment.
  const term = useDeferredValue(q.trim())
  const list = useQuery({
    queryKey: [...CUSTOMERS, term, page],
    queryFn: () => fetchCustomers({ q: term, page, page_size: PAGE_SIZE }),
    // The old page stays on screen while the next one loads, so the table never blinks
    // empty between keystrokes.
    placeholderData: keepPreviousData,
  })
  const total = list.data?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const first = (page - 1) * PAGE_SIZE + 1

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Input
          type="search"
          aria-label="Search clients"
          placeholder="Search by name, phone or email"
          className="w-full max-w-sm"
          value={q}
          onChange={(e) => {
            setQ(e.target.value)
            setPage(1)
          }}
        />
        {list.data && (
          <p className="ml-auto text-muted-foreground tabular-nums" aria-live="polite">
            {total === 1 ? '1 client' : `${total} clients`}
          </p>
        )}
      </div>

      {list.isPending ? (
        <Skeleton className="h-64 w-full" />
      ) : list.isError ? (
        <p role="alert" className="text-destructive">
          {list.error.message}
        </p>
      ) : list.data.customers.length === 0 ? (
        term ? (
          <EmptyState
            icon={SearchX}
            title="No clients match"
            description="Try fewer letters, or the first digits of the phone number."
          />
        ) : (
          <EmptyState
            icon={Users}
            title="No clients yet"
            description="Clients are added from the booking dialog when you book their first appointment."
          />
        )
      ) : (
        <div className="rounded-xl border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="pl-4">Name</TableHead>
                <TableHead>Phone</TableHead>
                <TableHead>Email</TableHead>
                <TableHead className="pr-4 text-right">Client since</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {list.data.customers.map((c) => (
                <TableRow
                  key={c.id}
                  className="h-10 cursor-pointer"
                  onClick={() => navigate(`/clients/${c.id}`)}
                >
                  <TableCell className="pl-4 font-medium">
                    <Link
                      to={`/clients/${c.id}`}
                      className="hover:underline"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {fullName(c)}
                    </Link>
                  </TableCell>
                  <TableCell className="tabular-nums">{c.phone ? formatPhone(c.phone) : blank}</TableCell>
                  <TableCell className="max-w-64 truncate">{c.email ?? blank}</TableCell>
                  <TableCell className="pr-4 text-right text-muted-foreground tabular-nums">
                    <time dateTime={c.created_at}>{shortDate(c.created_at)}</time>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {pages > 1 && (
            <div className="flex items-center justify-between border-t px-4 py-2 text-muted-foreground">
              <span className="tabular-nums">
                {first}–{Math.min(page * PAGE_SIZE, total)} of {total}
              </span>
              <div className="flex gap-1">
                <Button
                  variant="outline"
                  size="icon-sm"
                  aria-label="Previous page"
                  disabled={page <= 1}
                  onClick={() => setPage((p) => p - 1)}
                >
                  <ChevronLeft />
                </Button>
                <Button
                  variant="outline"
                  size="icon-sm"
                  aria-label="Next page"
                  disabled={page >= pages}
                  onClick={() => setPage((p) => p + 1)}
                >
                  <ChevronRight />
                </Button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

const PROFILE = ['customer-profile'] as const

/**
 * One client: who they are, how to reach them, and every visit past and upcoming with the
 * time in the business's zone. The whole page is one request, and on the server that request
 * is one access-log row — so the query is deliberately not refetched on focus and never
 * retried: each fetch is an audited access, and the browser should not generate them on
 * its own.
 */
export function ClientPage() {
  const { id = '' } = useParams()
  const profile = useQuery({
    queryKey: [...PROFILE, id],
    queryFn: () => fetchCustomerProfile(id),
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  })

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <Link
        to="/clients"
        className="inline-flex items-center gap-1.5 text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-4" aria-hidden />
        All clients
      </Link>

      {profile.isPending ? (
        <>
          <Skeleton className="h-36 w-full" />
          <Skeleton className="h-48 w-full" />
        </>
      ) : profile.isError ? (
        <EmptyState
          icon={UserRound}
          title={
            profile.error instanceof ApiError && profile.error.status === 404
              ? 'No such client'
              : 'Could not load this client'
          }
          description={
            profile.error instanceof ApiError && profile.error.status === 404
              ? 'This record does not exist, or was removed.'
              : profile.error.message
          }
        />
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle className="text-base font-medium">
                <h2>{fullName(profile.data.customer)}</h2>
              </CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="grid gap-x-8 gap-y-3 sm:grid-cols-3">
                <Field label="Phone">
                  {profile.data.customer.phone ? (
                    <a
                      href={`tel:${profile.data.customer.phone}`}
                      className="tabular-nums hover:underline"
                    >
                      {formatPhone(profile.data.customer.phone)}
                    </a>
                  ) : (
                    blank
                  )}
                </Field>
                <Field label="Email">
                  {profile.data.customer.email ? (
                    <a
                      href={`mailto:${profile.data.customer.email}`}
                      className="break-all hover:underline"
                    >
                      {profile.data.customer.email}
                    </a>
                  ) : (
                    blank
                  )}
                </Field>
                <Field label="Client since">
                  <time dateTime={profile.data.customer.created_at} className="tabular-nums">
                    {shortDate(profile.data.customer.created_at)}
                  </time>
                </Field>
              </dl>
            </CardContent>
          </Card>

          <section aria-labelledby="visits" className="space-y-2">
            <h2 id="visits" className="text-base font-medium">
              Appointments
            </h2>
            {profile.data.appointments.length === 0 ? (
              <EmptyState
                icon={UserRound}
                title="No appointments yet"
                description="Visits booked for this client, past and upcoming, appear here."
              />
            ) : (
              <div className="rounded-xl border bg-card">
                <Table aria-label="Appointments">
                  <TableHeader>
                    <TableRow>
                      <TableHead className="pl-4">When</TableHead>
                      <TableHead>Service</TableHead>
                      <TableHead>Practitioner</TableHead>
                      <TableHead className="pr-4">Status</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {profile.data.appointments.map((a) => (
                      <VisitRow key={a.id} visit={a} timezone={profile.data.timezone} />
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  )
}

const STATUS: Record<
  Visit['status'],
  { label: string; variant: 'info' | 'success' | 'outline' | 'warning' }
> = {
  confirmed: { label: 'Confirmed', variant: 'info' },
  completed: { label: 'Completed', variant: 'success' },
  cancelled: { label: 'Cancelled', variant: 'outline' },
  no_show: { label: 'No show', variant: 'warning' },
}

function VisitRow({ visit, timezone }: { visit: Visit; timezone: string }) {
  const status = STATUS[visit.status]
  return (
    <TableRow className="h-10">
      <TableCell className="pl-4 tabular-nums">
        <time dateTime={visit.starts_at}>{whenAt(visit.starts_at, timezone)}</time>
      </TableCell>
      <TableCell>{visit.service.name}</TableCell>
      <TableCell>{visit.staff.display_name}</TableCell>
      <TableCell className="pr-4">
        <Badge variant={status.variant}>{status.label}</Badge>
      </TableCell>
    </TableRow>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
      <dd className="mt-0.5">{children}</dd>
    </div>
  )
}

const blank = <span className="text-muted-foreground">—</span>

function fullName(c: CustomerRecord): string {
  return `${c.first_name} ${c.last_name}`
}

/** `5 Jan 2026`: the day a record was made needs no clock. Browser zone: it is a creation
 *  stamp, not an appointment. */
function shortDate(instant: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date(instant))
}

/** `Mon, 5 Oct 2026, 10:00 AM` in the business's zone — the clock the visit happened on. */
function whenAt(instant: string, timezone: string): string {
  return new Intl.DateTimeFormat(undefined, {
    weekday: 'short',
    dateStyle: undefined,
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZone: timezone,
  }).format(new Date(instant))
}
