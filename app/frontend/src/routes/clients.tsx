import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Ellipsis,
  Eraser,
  History,
  Pencil,
  Plus,
  SearchX,
  ShieldCheck,
  ShieldOff,
  TriangleAlert,
  UserRound,
  Users,
} from 'lucide-react'
import { useDeferredValue, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { toast } from 'sonner'
import { ClassificationBadge } from '@/components/classification-badge'
import { ClientFormsCard, type ClientFormsCardHandle } from '@/components/client-forms'
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import {
  ApiError,
  createCustomer,
  fetchAccessLog,
  fetchCustomerCompliance,
  fetchCustomerProfile,
  fetchCustomers,
  fieldErrors,
  requestErasure,
  updateCustomer,
  type AccessEntry,
  type ComplianceEntry,
  type Customer,
  type CustomerDetail,
  type Erasure,
  type CustomerDraft,
  type CustomerPatch,
  type Visit,
} from '@/lib/api'
import { useSession } from '@/lib/auth'
import { formatPhone } from '@/lib/phone'
import { invalidateScheduling } from '@/lib/query-client'
import { COMPLIANCE, CUSTOMER_PROFILE as PROFILE, CUSTOMERS } from '@/lib/query-keys'

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
  const { user } = useSession()
  const canCreate = user?.capabilities.includes('customers.manage') ?? false
  const [creating, setCreating] = useState(false)
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
        <div className="ml-auto flex items-center gap-3">
          {list.data && (
            <p className="text-muted-foreground tabular-nums" aria-live="polite">
              {total === 1 ? '1 client' : `${total} clients`}
            </p>
          )}
          {canCreate && (
            <Button size="sm" onClick={() => setCreating(true)}>
              <Plus aria-hidden />
              New client
            </Button>
          )}
        </div>
      </div>

      {creating && (
        <NewClientDialog
          onClose={() => setCreating(false)}
          onCreated={(id) => navigate(`/clients/${id}`)}
        />
      )}

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

/**
 * Gap: clients could only be created from the booking dialog. `customers.manage`, the same
 * capability the booking dialog's inline "new client" needs, posts to the same
 * `POST /api/customers` the booking flow's `create_customer` serves either way — one place a
 * customer is made, whichever screen starts it — then opens the new profile.
 */
function NewClientDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: (id: string) => void
}) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState({ first_name: '', last_name: '', email: '', phone: '' })
  const [errors, setErrors] = useState<Record<string, string>>({})
  const set = (patch: Partial<typeof draft>) => setDraft((d) => ({ ...d, ...patch }))
  const create = useMutation({
    mutationFn: () => {
      const payload: CustomerDraft = {
        first_name: draft.first_name.trim(),
        last_name: draft.last_name.trim(),
        email: draft.email.trim() || null,
        phone: draft.phone.trim() || null,
      }
      return createCustomer(payload)
    },
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: CUSTOMERS })
      toast.success(`${fullName(created)} added`)
      onCreated(created.id)
    },
    onError: (error) => {
      const found = fieldErrors(error)
      if (
        Object.keys(found).length === 0 &&
        error instanceof ApiError &&
        error.status === 409
      ) {
        found.email = error.message
      }
      setErrors(found)
    },
  })
  const incomplete = !draft.first_name.trim() || !draft.last_name.trim()

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New client</DialogTitle>
          <DialogDescription>Added to the client list right away.</DialogDescription>
        </DialogHeader>
        <Form onSubmit={() => !incomplete && create.mutate()}>
          <div className="grid gap-5 sm:grid-cols-2">
            <FormField label="First name" htmlFor="new-client-first" error={errors.first_name}>
              <Input
                id="new-client-first"
                autoFocus
                required
                maxLength={100}
                value={draft.first_name}
                onChange={(e) => set({ first_name: e.target.value })}
              />
            </FormField>
            <FormField label="Last name" htmlFor="new-client-last" error={errors.last_name}>
              <Input
                id="new-client-last"
                required
                maxLength={100}
                value={draft.last_name}
                onChange={(e) => set({ last_name: e.target.value })}
              />
            </FormField>
          </div>
          <div className="grid gap-5 sm:grid-cols-2">
            <FormField label="Phone" htmlFor="new-client-phone" error={errors.phone}>
              <Input
                id="new-client-phone"
                value={draft.phone}
                onChange={(e) => set({ phone: e.target.value })}
              />
            </FormField>
            <FormField label="Email" htmlFor="new-client-email" error={errors.email}>
              <Input
                id="new-client-email"
                type="email"
                value={draft.email}
                onChange={(e) => set({ email: e.target.value })}
              />
            </FormField>
          </div>
          {create.error && Object.keys(errors).length === 0 && (
            <FormError>{create.error.message}</FormError>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending || incomplete}>
              {create.isPending ? 'Creating…' : 'Create client'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}

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
  const canAudit = user?.capabilities.includes('audit.view') ?? false
  const canSendForms = user?.capabilities.includes('forms.issue') ?? false
  const canViewForms = user?.capabilities.includes('forms.view') ?? false
  // An Admin Mode capability: offered only while the window is open, never as a refusal.
  const canErase =
    (user?.capabilities.includes('customers.erase') ?? false) && user?.mode === 'admin'
  const [editing, setEditing] = useState(false)
  const [erasing, setErasing] = useState(false)
  const formsCard = useRef<ClientFormsCardHandle>(null)
  const profile = useQuery({
    queryKey: [...PROFILE, id],
    queryFn: () => fetchCustomerProfile(id),
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  })
  // Metadata (template names and statuses, no answers) — `customers.view` reads it the same
  // way it reads the rest of the profile; a suppressed client has nothing to chase.
  const compliance = useQuery({
    queryKey: [...COMPLIANCE, id],
    queryFn: () => fetchCustomerCompliance(id),
    enabled: profile.isSuccess && !profile.data.customer.suppressed,
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
          {profile.data.customer.erasure && (
            <ErasureBanner erasure={profile.data.customer.erasure} />
          )}
          {compliance.data && compliance.data.length > 0 && (
            <ComplianceBanner
              entries={compliance.data}
              canSend={canSendForms}
              onSend={(templateId) => formsCard.current?.openSendFor(templateId)}
            />
          )}
          <Card>
            <CardHeader className="flex-row items-center justify-between">
              <CardTitle className="flex items-center gap-2 text-base font-medium">
                <h2>{fullName(profile.data.customer)}</h2>
                <ClassificationBadge classification={profile.data.customer.classification} />
              </CardTitle>
              <div className="flex items-center gap-1">
                {/* An erased tombstone is not refilled; a held chart still takes a DOB fix. */}
                {canEdit &&
                  (!profile.data.customer.suppressed || profile.data.customer.erasure?.held) && (
                    <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
                      <Pencil aria-hidden />
                      Edit details
                    </Button>
                  )}
                {canErase && !profile.data.customer.suppressed && (
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="ghost" size="icon-sm" aria-label="More actions">
                        <Ellipsis aria-hidden />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem variant="destructive" onSelect={() => setErasing(true)}>
                        <Eraser aria-hidden /> Request erasure
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                )}
              </div>
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
                <Field label="Records">
                  <RetentionLine retention={profile.data.customer.retention} />
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

          {(canSendForms || canViewForms) && (
            <ClientFormsCard
              ref={formsCard}
              customerId={id}
              timezone={profile.data.timezone}
              suppressed={profile.data.customer.suppressed}
              canSend={canSendForms}
              canView={canViewForms}
            />
          )}

          {editing && (
            <ClientEditDialog customer={profile.data.customer} onClose={() => setEditing(false)} />
          )}
          {erasing && (
            <ErasureDialog
              customer={profile.data.customer}
              appointments={profile.data.appointments}
              onClose={() => setErasing(false)}
            />
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

          {canAudit && <AccessHistory customerId={id} adminMode={user?.mode === 'admin'} />}
        </>
      )}
    </div>
  )
}

/**
 * What an erasure request will do, said before it is sent (ADR-0001 §3, pre-flight D5b). The
 * split is the chart's retention hold, which the profile already carries: held, and the name,
 * DOB and visits stay until the hold ends; not held, and only the visits stay, against an
 * anonymous record. Contact details, both contacts and notes go either way.
 *
 * Upcoming confirmed visits block the request (owner decision, M5): counted here from the
 * profile's own visit list, which already holds every appointment, and re-checked by the
 * server, whose 409 `upcoming_appointments` count wins when the two disagree.
 */
function ErasureDialog({
  customer,
  appointments,
  onClose,
}: {
  customer: CustomerDetail
  appointments: Visit[]
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [note, setNote] = useState('')
  // Held means what the server's `is_held` means: a live dated hold, or no DOB to date one.
  // An `expired` hold is not held — promising to keep a name the server erases is the one
  // thing this dialog must never do.
  const held = customer.retention.status === 'held' || customer.retention.status === 'needs_dob'
  const erase = useMutation({
    mutationFn: () => requestErasure(customer.id, note.trim() || null),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [...PROFILE, customer.id] })
      queryClient.invalidateQueries({ queryKey: CUSTOMERS })
      // Calendar cards carry the client's name and contacts; after an erasure they are stale.
      invalidateScheduling(queryClient)
      toast.success('Erasure recorded')
      onClose()
    },
    // The visit list was stale if the server counted differently; refetch it.
    onError: () => queryClient.invalidateQueries({ queryKey: [...PROFILE, customer.id] }),
  })
  const refused =
    erase.error instanceof ApiError && erase.error.code === 'upcoming_appointments'
      ? (erase.error.body as { count: number }).count
      : null
  // When the dialog opened: the count is a snapshot, and the server re-checks it anyway.
  const [now] = useState(() => Date.now())
  const upcoming =
    refused ??
    appointments.filter((a) => a.status === 'confirmed' && Date.parse(a.starts_at) > now).length
  const removed = [
    ...(held ? [] : ['Name — replaced with “Erased Client”', 'Date of birth']),
    'Email and phone',
    'Emergency and secondary contacts',
    'Notes',
    ...(held ? [] : ["The client's document key — every stored document becomes unreadable"]),
  ]
  const kept = held
    ? ['Name', 'Date of birth', 'Visit history']
    : ["Visit history, against an anonymous record — appointments are the business's own records"]
  const why =
    customer.retention.status === 'held' && customer.retention.expires_on
      ? `Regulated health record, held until ${formatDob(customer.retention.expires_on)}; destroyed automatically after that.`
      : customer.retention.status === 'held' || customer.retention.status === 'needs_dob'
        ? 'Regulated health record with no end date yet — held until a date of birth is recorded.'
        : customer.retention.status === 'expired' && customer.retention.expires_on
          ? `Retention period ended ${formatDob(customer.retention.expires_on)} — no longer held, so everything personal goes now.`
          : 'Not under a retention hold, so everything personal goes now.'

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Erase this client?</DialogTitle>
          <DialogDescription>
            {fullName(customer)} will disappear from the client list, search and booking. This
            cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <p className="text-sm">{why}</p>
        <div className="grid gap-4 text-sm sm:grid-cols-2">
          <div>
            <h3 id="erase-removed" className="font-medium">
              Removed now
            </h3>
            <ul aria-labelledby="erase-removed" className="mt-1 list-disc space-y-0.5 pl-5">
              {removed.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
          <div>
            <h3 id="erase-kept" className="font-medium">
              Kept
            </h3>
            <ul aria-labelledby="erase-kept" className="mt-1 list-disc space-y-0.5 pl-5">
              {kept.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        </div>
        <FormField label="Note (optional)" htmlFor="erase-note">
          <Textarea
            id="erase-note"
            maxLength={2000}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="How the client asked"
          />
        </FormField>
        {upcoming > 0 ? (
          <p role="alert" className="text-sm font-medium text-destructive">
            This client has {upcoming} upcoming appointment(s). Cancel or complete them before
            requesting erasure.
          </p>
        ) : (
          erase.isError && <FormError>{erase.error.message}</FormError>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            {upcoming > 0 ? 'Close' : 'Cancel'}
          </Button>
          {upcoming === 0 && (
            <Button
              type="button"
              variant="destructive"
              disabled={erase.isPending}
              onClick={() => erase.mutate()}
            >
              Erase client
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** After an erasure request: what was kept and why, in the words the client was given. */
const COMPLIANCE_LABEL: Record<ComplianceEntry['status'], string> = {
  missing: 'Missing',
  expired: 'Expired',
  resign_required: 'Needs a new signature',
}

/**
 * Task 8 (#51): the essential forms this client is missing, expired on, or must re-sign —
 * metadata, not PHI, the same reason opening the profile stays one access-log row. "Send
 * form" reaches into the Forms card below through a ref so the dialog opens with this one
 * already chosen, rather than asking the front desk to find it again in a list.
 */
function ComplianceBanner({
  entries,
  canSend,
  onSend,
}: {
  entries: ComplianceEntry[]
  canSend: boolean
  onSend: (templateId: string) => void
}) {
  return (
    <div
      role="status"
      aria-label="Essential forms outstanding"
      className="flex items-start gap-2 rounded-xl border border-warning/30 bg-warning/5 p-4 text-sm"
    >
      <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-warning" />
      <div className="flex-1 space-y-2">
        <p className="font-medium">
          {entries.length === 1 ? 'An essential form needs attention' : 'Essential forms need attention'}
        </p>
        <ul className="space-y-1.5">
          {entries.map((entry) => (
            <li key={entry.template_id} className="flex flex-wrap items-center gap-2">
              <span>{entry.name}</span>
              <Badge variant="warning">{COMPLIANCE_LABEL[entry.status]}</Badge>
              {canSend && (
                <Button
                  size="sm"
                  variant="outline"
                  className="ml-auto"
                  onClick={() => onSend(entry.template_id)}
                >
                  Send form
                </Button>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}

function ErasureBanner({ erasure }: { erasure: Erasure }) {
  return (
    <div
      role="status"
      aria-label="Erasure requested"
      className="flex items-start gap-2 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm"
    >
      <ShieldOff aria-hidden className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div className="space-y-1">
        <p className="font-medium">
          Erasure requested on{' '}
          <time dateTime={erasure.requested_at}>{shortDate(erasure.requested_at)}</time>
        </p>
        {erasure.held ? (
          <p>
            Retained: {erasure.retained.join(', ')}. {erasure.held_reason}.
          </p>
        ) : erasure.purged_at === null ? (
          // Not held, not finished: a hold that has since ended, or a purge still queued. The
          // nightly job finishes either, so nothing is promised gone before it has run.
          <p>
            Not under a retention hold. Anything personal still on this record is removed by
            tonight at the latest; visit history stays against an anonymous record.
          </p>
        ) : (
          <p>
            Nothing personal is retained. Visit history stays against an anonymous record,
            because appointments are the business's own records.
          </p>
        )}
        <p className="text-muted-foreground">
          This client is hidden from the client list, search and booking.
        </p>
      </div>
    </div>
  )
}

const ACCESS_LOG = ['customer-access-log'] as const
const ACCESS_PAGE_SIZE = 25

/** What an entry was, in words. Forms and session notes will add their own resource types,
 *  and an unknown one still reads as its raw key. */
const OPENED: Record<string, string> = {
  customer_profile: 'Opened profile',
  customer_erasure: 'Requested erasure',
}

/**
 * Who opened this record, when, as what and from where (ADR-0002 §6). Shown to holders of
 * `audit.view` only — hidden otherwise, like Settings in the nav — and asked for only in
 * Admin Mode: the capability is an administrative one, and a Staff Mode request would be
 * a refusal the card can predict. The date range is the server's (`from`/`to`, business-
 * local, 90 days by default), so the inputs show whatever range was actually applied.
 */
function AccessHistory({ customerId, adminMode }: { customerId: string; adminMode: boolean }) {
  const [range, setRange] = useState<{ from?: string; to?: string }>({})
  const [page, setPage] = useState(1)
  const report = useQuery({
    queryKey: [...ACCESS_LOG, customerId, range.from, range.to, page],
    queryFn: () =>
      fetchAccessLog(customerId, { ...range, page, page_size: ACCESS_PAGE_SIZE }),
    enabled: adminMode,
    placeholderData: keepPreviousData,
  })
  const from = range.from ?? report.data?.from ?? ''
  const to = range.to ?? report.data?.to ?? ''
  const total = report.data?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / ACCESS_PAGE_SIZE))
  const first = (page - 1) * ACCESS_PAGE_SIZE + 1
  const choose = (next: { from?: string; to?: string }) => {
    setRange({ from, to, ...next })
    setPage(1)
  }

  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-end justify-between gap-3">
        <CardTitle className="text-base font-medium">
          <h2>Access history</h2>
        </CardTitle>
        {adminMode && (
          <div className="flex items-end gap-2">
            <div className="grid gap-1">
              <Label htmlFor="access-from" className="text-xs text-muted-foreground">
                From
              </Label>
              <Input
                id="access-from"
                type="date"
                className="w-40"
                value={from}
                max={to || undefined}
                onChange={(e) => choose({ from: e.target.value })}
              />
            </div>
            <div className="grid gap-1">
              <Label htmlFor="access-to" className="text-xs text-muted-foreground">
                To
              </Label>
              <Input
                id="access-to"
                type="date"
                className="w-40"
                value={to}
                min={from || undefined}
                onChange={(e) => choose({ to: e.target.value })}
              />
            </div>
          </div>
        )}
      </CardHeader>
      <CardContent>
        {!adminMode ? (
          <p className="flex items-center gap-2 text-muted-foreground">
            <ShieldCheck className="size-4" aria-hidden />
            Switch to Admin Mode to see who has opened this record.
          </p>
        ) : report.isPending ? (
          <Skeleton className="h-40 w-full" />
        ) : report.isError ? (
          <p role="alert" className="text-destructive">
            {report.error.message}
          </p>
        ) : report.data.entries.length === 0 ? (
          <EmptyState
            icon={History}
            title="Nobody opened this record in these dates"
            description="Every time someone opens this client's profile, it is listed here."
          />
        ) : (
          // The old range stays on screen while the new one loads; dimmed, so it is not
          // read as the answer to the dates now in the inputs.
          <div
            className={`rounded-xl border transition-opacity ${report.isPlaceholderData ? 'opacity-60' : ''}`}
            aria-busy={report.isPlaceholderData}
          >
            <Table aria-label="Access history">
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-4">When</TableHead>
                  <TableHead>Who</TableHead>
                  <TableHead>Role</TableHead>
                  <TableHead>What</TableHead>
                  <TableHead className="pr-4">IP address</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {report.data.entries.map((entry) => (
                  <AccessRow key={entry.id} entry={entry} timezone={report.data.timezone} />
                ))}
              </TableBody>
            </Table>
            {pages > 1 && (
              <div className="flex items-center justify-between border-t px-4 py-2 text-muted-foreground">
                <span className="tabular-nums">
                  {first}–{Math.min(page * ACCESS_PAGE_SIZE, total)} of {total}
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
      </CardContent>
    </Card>
  )
}

function AccessRow({ entry, timezone }: { entry: AccessEntry; timezone: string }) {
  return (
    <TableRow className="h-10">
      <TableCell className="pl-4 tabular-nums">
        <time dateTime={entry.occurred_at}>{whenAt(entry.occurred_at, timezone)}</time>
      </TableCell>
      <TableCell className="max-w-48 truncate">{entry.actor_name}</TableCell>
      <TableCell>{entry.actor_role}</TableCell>
      <TableCell>{OPENED[entry.resource_type] ?? `${entry.action} ${entry.resource_type}`}</TableCell>
      <TableCell className="pr-4 tabular-nums">{entry.ip ?? blank}</TableCell>
    </TableRow>
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

/** When this chart may be destroyed (ADR-0001). A chart with no date of birth is held
 *  indefinitely — never "assumed adult" — and the fix is on this page, so it says what to do. */
function RetentionLine({ retention }: { retention: CustomerDetail['retention'] }) {
  if (retention.status === 'held') {
    // A held record with no date should never arrive; if it does, it still reads as held.
    return retention.expires_on ? (
      <span className="tabular-nums">Held until {formatDob(retention.expires_on)}</span>
    ) : (
      <Badge variant="warning">Held — expiry date unavailable</Badge>
    )
  }
  if (retention.status === 'expired' && retention.expires_on) {
    return (
      <span className="tabular-nums text-muted-foreground">
        Retention period ended {formatDob(retention.expires_on)} — no longer held
      </span>
    )
  }
  if (retention.status === 'needs_dob') {
    return <Badge variant="warning">Retention cannot be computed — add a date of birth</Badge>
  }
  if (retention.status === 'not_held') {
    return <span className="text-muted-foreground">Not under a retention hold</span>
  }
  // An unknown status is never read as "free to delete".
  return <Badge variant="warning">Retention status unknown</Badge>
}

function fullName(c: Customer): string {
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

/** `date_of_birth` (and a retention `expires_on`) is a bare `YYYY-MM-DD`, with no clock and no zone — read its digits
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

/** The browser's local date, as `YYYY-MM-DD` — not `toISOString()`, which is UTC and prints
 *  tomorrow's date for anyone west of Greenwich after roughly 8pm local: exactly the window
 *  in which today's own real DOB would fail the "not in the future" check it feeds. */
function todayISO(): string {
  const today = new Date()
  const month = String(today.getMonth() + 1).padStart(2, '0')
  const day = String(today.getDate()).padStart(2, '0')
  return `${today.getFullYear()}-${month}-${day}`
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
 *
 * After an erasure the server takes one edit only — a DOB correction on a chart still held
 * (`update_customer`) — so a suppressed client gets that one field and nothing else.
 */
function ClientEditDialog({ customer, onClose }: { customer: CustomerDetail; onClose: () => void }) {
  const queryClient = useQueryClient()
  const original = toEditable(customer)
  const [draft, setDraft] = useState(original)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const set = (patch: Partial<EditableCustomer>) => setDraft((d) => ({ ...d, ...patch }))

  const dobOnly = customer.suppressed
  const save = useMutation({
    mutationFn: () => updateCustomer(customer.id, diffPatch(original, draft)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [...PROFILE, customer.id] })
      queryClient.invalidateQueries({ queryKey: CUSTOMERS })
      invalidateScheduling(queryClient)
      toast.success('Client details saved')
      onClose()
    },
    onError: (error) => {
      const found = fieldErrors(error)
      // In the DOB-only form a 409 is the suppression refusal, shown as a form error.
      if (
        !dobOnly &&
        Object.keys(found).length === 0 &&
        error instanceof ApiError &&
        error.status === 409
      ) {
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
          <DialogTitle>{dobOnly ? 'Correct date of birth' : 'Edit details'}</DialogTitle>
          <DialogDescription>
            {dobOnly
              ? 'Erasure was requested, so only the date of birth can be corrected — it decides how long the record is held.'
              : 'Contact information, emergency and secondary contacts, and front-desk notes.'}
          </DialogDescription>
        </DialogHeader>

        <Form onSubmit={() => !incomplete && save.mutate()}>
          {!dobOnly && (
            <>
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
            </>
          )}

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

          {!dobOnly && (
            <>
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
            </>
          )}

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
