import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ChevronLeft, ChevronRight, Pencil, SearchX, UserRound, Users } from 'lucide-react'
import { useDeferredValue, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { toast } from 'sonner'
import { ClassificationBadge } from '@/components/classification-badge'
import { EmptyState } from '@/components/empty-state'
import { Field as FormField, Form, FormError } from '@/components/form'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import {
  ApiError,
  fetchCustomerProfile,
  fetchCustomers,
  fieldErrors,
  updateCustomer,
  type CustomerDetail,
  type CustomerPatch,
  type CustomerRecord,
  type Visit,
} from '@/lib/api'
import { useSession } from '@/lib/auth'
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
                <TableHead>Classification</TableHead>
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
                  <TableCell>
                    <ClassificationBadge classification={c.classification} />
                  </TableCell>
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
  const { user } = useSession()
  const canEdit = user?.capabilities.includes('customers.manage') ?? false
  const [editing, setEditing] = useState(false)
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
            <CardHeader className="flex-row items-center justify-between">
              <CardTitle className="flex items-center gap-2 text-base font-medium">
                <h2>{fullName(profile.data.customer)}</h2>
                <ClassificationBadge classification={profile.data.customer.classification} />
              </CardTitle>
              {canEdit && (
                <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
                  <Pencil aria-hidden />
                  Edit details
                </Button>
              )}
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
                <Field label="Date of birth">
                  {profile.data.customer.date_of_birth ? (
                    <span className="tabular-nums">
                      {formatDob(profile.data.customer.date_of_birth)}{' '}
                      <span className="text-muted-foreground">
                        ({ageFrom(profile.data.customer.date_of_birth)})
                      </span>
                    </span>
                  ) : (
                    blank
                  )}
                </Field>
              </dl>
            </CardContent>
          </Card>

          <div className="grid gap-4 sm:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle className="text-sm font-medium">Emergency contact</CardTitle>
              </CardHeader>
              <CardContent>
                <dl className="grid gap-y-3">
                  <Field label="Name">{profile.data.customer.emergency_contact_name ?? blank}</Field>
                  <Field label="Relationship">
                    {profile.data.customer.emergency_contact_relationship ?? blank}
                  </Field>
                  <Field label="Phone">
                    {profile.data.customer.emergency_contact_phone ? (
                      <a
                        href={`tel:${profile.data.customer.emergency_contact_phone}`}
                        className="tabular-nums hover:underline"
                      >
                        {formatPhone(profile.data.customer.emergency_contact_phone)}
                      </a>
                    ) : (
                      blank
                    )}
                  </Field>
                </dl>
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="text-sm font-medium">Secondary contact</CardTitle>
              </CardHeader>
              <CardContent>
                <dl className="grid gap-y-3">
                  <Field label="Name">{profile.data.customer.secondary_contact_name ?? blank}</Field>
                  <Field label="Phone">
                    {profile.data.customer.secondary_contact_phone ? (
                      <a
                        href={`tel:${profile.data.customer.secondary_contact_phone}`}
                        className="tabular-nums hover:underline"
                      >
                        {formatPhone(profile.data.customer.secondary_contact_phone)}
                      </a>
                    ) : (
                      blank
                    )}
                  </Field>
                  <Field label="Email">
                    {profile.data.customer.secondary_contact_email ? (
                      <a
                        href={`mailto:${profile.data.customer.secondary_contact_email}`}
                        className="break-all hover:underline"
                      >
                        {profile.data.customer.secondary_contact_email}
                      </a>
                    ) : (
                      blank
                    )}
                  </Field>
                </dl>
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle className="text-sm font-medium">Notes</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="whitespace-pre-wrap text-sm">
                {profile.data.customer.notes ?? (
                  <span className="text-muted-foreground">
                    No notes. Front-desk notes only — not a clinical record.
                  </span>
                )}
              </p>
            </CardContent>
          </Card>

          {editing && (
            <ClientEditDialog customer={profile.data.customer} onClose={() => setEditing(false)} />
          )}

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

/** `date_of_birth` is a bare `YYYY-MM-DD`, with no clock and no zone — read its digits
 *  directly rather than through `Date`'s local-timezone parsing, which can print the day
 *  before in any zone west of UTC. */
function formatDob(iso: string): string {
  const [y, m, d] = iso.split('-').map(Number)
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeZone: 'UTC' }).format(
    new Date(Date.UTC(y, m - 1, d)),
  )
}

function ageFrom(iso: string): number {
  const [y, m, d] = iso.split('-').map(Number)
  const today = new Date()
  const hadBirthdayThisYear =
    today.getMonth() + 1 > m || (today.getMonth() + 1 === m && today.getDate() >= d)
  return today.getFullYear() - y - (hadBirthdayThisYear ? 0 : 1)
}

function todayISO(): string {
  return new Date().toISOString().slice(0, 10)
}

/** The dialog's own shape: every field as a plain string, so a text input never has to
 *  special-case `null`. `toEditable` and `diffPatch` are the only places that cross between
 *  this and `CustomerPatch`'s `string | null`. */
type EditableCustomer = {
  first_name: string
  last_name: string
  email: string
  phone: string
  date_of_birth: string
  emergency_contact_name: string
  emergency_contact_phone: string
  emergency_contact_relationship: string
  secondary_contact_name: string
  secondary_contact_phone: string
  secondary_contact_email: string
  notes: string
}

function toEditable(c: CustomerDetail): EditableCustomer {
  return {
    first_name: c.first_name,
    last_name: c.last_name,
    email: c.email ?? '',
    phone: c.phone ?? '',
    date_of_birth: c.date_of_birth ?? '',
    emergency_contact_name: c.emergency_contact_name ?? '',
    emergency_contact_phone: c.emergency_contact_phone ?? '',
    emergency_contact_relationship: c.emergency_contact_relationship ?? '',
    secondary_contact_name: c.secondary_contact_name ?? '',
    secondary_contact_phone: c.secondary_contact_phone ?? '',
    secondary_contact_email: c.secondary_contact_email ?? '',
    notes: c.notes ?? '',
  }
}

/** Only what actually changed, so the request the server sees — and audits by field name —
 *  is the same set of fields the person actually touched (`exclude_unset` on the server
 *  side reads this the same way `CustomerPatch`'s keys are read here). */
function diffPatch(original: EditableCustomer, draft: EditableCustomer): CustomerPatch {
  const patch: CustomerPatch = {}
  if (draft.first_name.trim() !== original.first_name) patch.first_name = draft.first_name.trim()
  if (draft.last_name.trim() !== original.last_name) patch.last_name = draft.last_name.trim()
  if (draft.email.trim() !== original.email) patch.email = draft.email.trim() || null
  if (draft.phone.trim() !== original.phone) patch.phone = draft.phone.trim() || null
  if (draft.date_of_birth !== original.date_of_birth) {
    patch.date_of_birth = draft.date_of_birth || null
  }
  if (draft.emergency_contact_name.trim() !== original.emergency_contact_name) {
    patch.emergency_contact_name = draft.emergency_contact_name.trim() || null
  }
  if (draft.emergency_contact_phone.trim() !== original.emergency_contact_phone) {
    patch.emergency_contact_phone = draft.emergency_contact_phone.trim() || null
  }
  if (draft.emergency_contact_relationship.trim() !== original.emergency_contact_relationship) {
    patch.emergency_contact_relationship = draft.emergency_contact_relationship.trim() || null
  }
  if (draft.secondary_contact_name.trim() !== original.secondary_contact_name) {
    patch.secondary_contact_name = draft.secondary_contact_name.trim() || null
  }
  if (draft.secondary_contact_phone.trim() !== original.secondary_contact_phone) {
    patch.secondary_contact_phone = draft.secondary_contact_phone.trim() || null
  }
  if (draft.secondary_contact_email.trim() !== original.secondary_contact_email) {
    patch.secondary_contact_email = draft.secondary_contact_email.trim() || null
  }
  if (draft.notes.trim() !== original.notes) patch.notes = draft.notes.trim() || null
  return patch
}

const MIN_DOB = '1900-01-01'

/**
 * Contacts, DOB and notes, in one dialog. Submits only what changed (`diffPatch`) — the
 * server's own audit event lists exactly those field names, and a resubmit of an untouched
 * field would otherwise show up in that trail as a change nobody made. Server errors land
 * under the field FastAPI named in `loc`; a duplicate email (409, no `loc`) is put under
 * the email field by hand, since that is the one field a 409 here is ever about.
 */
function ClientEditDialog({ customer, onClose }: { customer: CustomerDetail; onClose: () => void }) {
  const queryClient = useQueryClient()
  const original = toEditable(customer)
  const [draft, setDraft] = useState(original)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const set = (patch: Partial<EditableCustomer>) => setDraft((d) => ({ ...d, ...patch }))

  const save = useMutation({
    mutationFn: () => updateCustomer(customer.id, diffPatch(original, draft)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [...PROFILE, customer.id] })
      queryClient.invalidateQueries({ queryKey: CUSTOMERS })
      toast.success('Client details saved')
      onClose()
    },
    onError: (error) => {
      const found = fieldErrors(error)
      if (Object.keys(found).length === 0 && error instanceof ApiError && error.status === 409) {
        found.email = error.message
      }
      setErrors(found)
    },
  })

  const validateDob = () => {
    setErrors((e) => {
      const { date_of_birth: _dropped, ...rest } = e
      if (!draft.date_of_birth) return rest
      if (draft.date_of_birth > todayISO()) {
        return { ...rest, date_of_birth: 'Date of birth cannot be in the future.' }
      }
      if (draft.date_of_birth < MIN_DOB) {
        return { ...rest, date_of_birth: 'Date of birth cannot be before 1900.' }
      }
      return rest
    })
  }

  const incomplete = !draft.first_name.trim() || !draft.last_name.trim()

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Edit details</DialogTitle>
          <DialogDescription>
            Contact information, emergency and secondary contacts, and front-desk notes.
          </DialogDescription>
        </DialogHeader>

        <Form onSubmit={() => !incomplete && save.mutate()}>
          <div className="grid gap-5 sm:grid-cols-2">
            <FormField label="First name" htmlFor="client-first" error={errors.first_name}>
              <Input
                id="client-first"
                required
                maxLength={100}
                value={draft.first_name}
                onChange={(e) => set({ first_name: e.target.value })}
              />
            </FormField>
            <FormField label="Last name" htmlFor="client-last" error={errors.last_name}>
              <Input
                id="client-last"
                required
                maxLength={100}
                value={draft.last_name}
                onChange={(e) => set({ last_name: e.target.value })}
              />
            </FormField>
          </div>

          <div className="grid gap-5 sm:grid-cols-2">
            <FormField label="Phone" htmlFor="client-phone" error={errors.phone}>
              <Input
                id="client-phone"
                value={draft.phone}
                onChange={(e) => set({ phone: e.target.value })}
              />
            </FormField>
            <FormField label="Email" htmlFor="client-email" error={errors.email}>
              <Input
                id="client-email"
                type="email"
                value={draft.email}
                onChange={(e) => set({ email: e.target.value })}
              />
            </FormField>
          </div>

          <FormField label="Date of birth" htmlFor="client-dob" error={errors.date_of_birth}>
            <Input
              id="client-dob"
              type="date"
              max={todayISO()}
              min={MIN_DOB}
              value={draft.date_of_birth}
              onChange={(e) => set({ date_of_birth: e.target.value })}
              onBlur={validateDob}
            />
          </FormField>

          <fieldset className="flex flex-col gap-4 rounded-xl border border-input p-4">
            <legend className="px-1 text-sm font-medium">Emergency contact</legend>
            <div className="grid gap-5 sm:grid-cols-2">
              <FormField
                label="Name"
                htmlFor="emergency-contact-name"
                error={errors.emergency_contact_name}
              >
                <Input
                  id="emergency-contact-name"
                  value={draft.emergency_contact_name}
                  onChange={(e) => set({ emergency_contact_name: e.target.value })}
                />
              </FormField>
              <FormField
                label="Relationship"
                htmlFor="emergency-contact-relationship"
                error={errors.emergency_contact_relationship}
              >
                <Input
                  id="emergency-contact-relationship"
                  value={draft.emergency_contact_relationship}
                  onChange={(e) => set({ emergency_contact_relationship: e.target.value })}
                />
              </FormField>
            </div>
            <FormField
              label="Phone"
              htmlFor="emergency-contact-phone"
              error={errors.emergency_contact_phone}
            >
              <Input
                id="emergency-contact-phone"
                value={draft.emergency_contact_phone}
                onChange={(e) => set({ emergency_contact_phone: e.target.value })}
              />
            </FormField>
          </fieldset>

          <fieldset className="flex flex-col gap-4 rounded-xl border border-input p-4">
            <legend className="px-1 text-sm font-medium">Secondary contact</legend>
            <div className="grid gap-5 sm:grid-cols-2">
              <FormField
                label="Name"
                htmlFor="secondary-contact-name"
                error={errors.secondary_contact_name}
              >
                <Input
                  id="secondary-contact-name"
                  value={draft.secondary_contact_name}
                  onChange={(e) => set({ secondary_contact_name: e.target.value })}
                />
              </FormField>
              <FormField
                label="Phone"
                htmlFor="secondary-contact-phone"
                error={errors.secondary_contact_phone}
              >
                <Input
                  id="secondary-contact-phone"
                  value={draft.secondary_contact_phone}
                  onChange={(e) => set({ secondary_contact_phone: e.target.value })}
                />
              </FormField>
            </div>
            <FormField
              label="Email"
              htmlFor="secondary-contact-email"
              error={errors.secondary_contact_email}
            >
              <Input
                id="secondary-contact-email"
                type="email"
                value={draft.secondary_contact_email}
                onChange={(e) => set({ secondary_contact_email: e.target.value })}
              />
            </FormField>
          </fieldset>

          <FormField
            label="Notes"
            htmlFor="client-notes"
            error={errors.notes}
            hint="Front-desk notes — not a clinical record."
          >
            <Textarea
              id="client-notes"
              rows={3}
              maxLength={2000}
              value={draft.notes}
              onChange={(e) => set({ notes: e.target.value })}
            />
          </FormField>

          {save.error && Object.keys(errors).length === 0 && <FormError>{save.error.message}</FormError>}

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || incomplete}>
              {save.isPending ? 'Saving…' : 'Save changes'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
