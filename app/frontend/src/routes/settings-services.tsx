import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { MoreHorizontal, Pencil, Plus, Power, PowerOff, Scissors, X } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { EmptyState } from '@/components/empty-state'
import { Field, Form, FormError } from '@/components/form'
import { Badge } from '@/components/ui/badge'
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
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
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import {
  ApiError,
  createService,
  deactivateService,
  fetchResources,
  fetchServices,
  fetchStaff,
  fetchStaffPalette,
  reactivateService,
  replaceServiceRequirements,
  replaceServiceStaff,
  updateService,
  type ResourceKind,
  type ResourceRow,
  type ServiceDraft,
  type ServiceRequirement,
  type ServiceRow,
  type StaffColour,
  type StaffRow,
} from '@/lib/api'
import { centsToDollars, dollarsToCents } from '@/lib/money'
import { RESOURCES, SERVICES, STAFF, STAFF_PALETTE } from '@/lib/query-keys'

/**
 * Settings → Services: the catalog, and what delivering each thing in it needs (PRD §2, §7).
 *
 * **This screen is where the availability engine gets its inputs.** The duration and the two
 * buffers are the width it slides across a staff member's day; the eligible staff are whose
 * day it looks at; the requirements are the rooms and devices whose free intervals that has
 * to be intersected with (tech-stack §19). A service requiring "any treatment room plus
 * laser unit 2" is what later lets a slot be refused because the device is busy although the
 * practitioner and every room are free.
 *
 * **Price is typed in dollars and stored in cents.** `lib/money.ts` is the only place the
 * two meet, and it converts from the string rather than from a float precisely so $12.005
 * rounds up rather than down.
 *
 * **Buffers are not part of the duration.** The block a client books is the duration; the
 * turnaround either side of it is time nobody booked, which is why they are three fields.
 */
export function ServicesPanel() {
  const [includeInactive, setIncludeInactive] = useState(false)
  const services = useQuery({
    queryKey: [...SERVICES, includeInactive],
    queryFn: () => fetchServices(includeInactive),
    // Ticking the filter is a different query; without this the table flashes to a skeleton
    // for the length of one round trip, over a checkbox.
    placeholderData: (previous) => previous,
  })
  // The three lists the dialog picks from. Read here rather than inside it so opening the
  // dialog is instant and so the table can name a service's staff and resources too.
  const staff = useQuery({ queryKey: STAFF, queryFn: () => fetchStaff() })
  const resources = useQuery({ queryKey: RESOURCES, queryFn: () => fetchResources() })
  const palette = useQuery({ queryKey: STAFF_PALETTE, queryFn: fetchStaffPalette })
  const [editing, setEditing] = useState<ServiceRow | null>(null)
  const [creating, setCreating] = useState(false)

  const pending = [services, staff, resources, palette].some((q) => q.isPending)
  const failed = [services, staff, resources, palette].find((q) => q.isError)
  if (pending) return <Skeleton className="h-64 w-full" />
  if (failed) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {failed.error?.message}
      </p>
    )
  }

  const dialog = {
    staff: staff.data ?? [],
    resources: resources.data ?? [],
    palette: palette.data ?? [],
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <p className="max-w-3xl text-sm text-muted-foreground">
          Treatments, consultations, classes — each with how long it takes, the turnaround
          either side of it, what it costs, and the room or equipment it needs.
        </p>
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <Checkbox
              id="show-inactive-services"
              checked={includeInactive}
              onCheckedChange={(on) => setIncludeInactive(on === true)}
            />
            <Label htmlFor="show-inactive-services" className="font-normal">
              Show inactive
            </Label>
          </div>
          {/* Hidden while the empty state is showing: that block carries the same button,
              and one screen offering the same primary action twice is one too many. */}
          {services.data?.length !== 0 && (
            <Button onClick={() => setCreating(true)}>
              <Plus aria-hidden />
              Add service
            </Button>
          )}
        </div>
      </div>

      {services.data?.length === 0 ? (
        <EmptyState
          icon={Scissors}
          title="No services yet"
          description="Add what the business sells. Bookings and the availability search both read this list."
          action={
            <Button onClick={() => setCreating(true)}>
              <Plus aria-hidden />
              Add service
            </Button>
          }
        />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Duration</TableHead>
              <TableHead>Buffers</TableHead>
              <TableHead className="text-right">Price</TableHead>
              <TableHead>Booking</TableHead>
              <TableHead>Delivered by</TableHead>
              <TableHead>Needs</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {services.data?.map((service) => (
              <ServiceLine
                key={service.id}
                service={service}
                onEdit={() => setEditing(service)}
              />
            ))}
          </TableBody>
        </Table>
      )}

      {creating && <ServiceDialog {...dialog} onClose={() => setCreating(false)} />}
      {editing && (
        <ServiceDialog {...dialog} service={editing} onClose={() => setEditing(null)} />
      )}
    </div>
  )
}

/** "Any space", or the one resource named. What the table prints and the builder echoes. */
function requirementLabel(requirement: { kind: ResourceKind; resource_name: string | null }) {
  if (requirement.resource_name) return requirement.resource_name
  return requirement.kind === 'space' ? 'Any space' : 'Any equipment'
}

function ServiceLine({ service, onEdit }: { service: ServiceRow; onEdit: () => void }) {
  const queryClient = useQueryClient()
  const refresh = () => queryClient.invalidateQueries({ queryKey: SERVICES })

  const setActive = useMutation({
    mutationFn: (active: boolean) =>
      active ? reactivateService(service.id) : deactivateService(service.id),
    onSuccess: (updated) => {
      toast.success(
        updated.active ? `${updated.name} can be booked again` : `Deactivated ${updated.name}`,
        updated.active
          ? undefined
          : { description: 'Appointments already booked against it are kept as they are.' },
      )
      refresh()
    },
    onError: (error) => {
      toast.error(error.message)
      refresh()
    },
  })

  const buffers =
    service.buffer_before_minutes || service.buffer_after_minutes
      ? `+${service.buffer_before_minutes} / +${service.buffer_after_minutes} min`
      : '—'

  return (
    // Inactive rows are greyed rather than hidden when the filter is on — they are history,
    // and history that looks identical to the catalog is the wrong kind of quiet.
    <TableRow className={service.active ? undefined : 'opacity-55'}>
      <TableCell>
        <span className="font-medium">{service.name}</span>
        {service.description && (
          <span className="block max-w-xs truncate text-xs text-muted-foreground">
            {service.description}
          </span>
        )}
      </TableCell>
      <TableCell className="tabular-nums">{service.duration_minutes} min</TableCell>
      <TableCell className="tabular-nums text-muted-foreground">{buffers}</TableCell>
      <TableCell className="text-right tabular-nums">
        ${centsToDollars(service.price_cents)}
      </TableCell>
      <TableCell>
        {service.bookable_online ? (
          <Badge variant="info">Online</Badge>
        ) : (
          <Badge variant="outline">Staff only</Badge>
        )}
      </TableCell>
      <TableCell className="tabular-nums">
        {service.staff_ids.length === 0 ? (
          // Not an error — a service nobody is cleared for is simply one nothing can be
          // booked into yet, and saying so is more use than a bare zero.
          <span className="text-muted-foreground">Nobody yet</span>
        ) : (
          `${service.staff_ids.length} staff`
        )}
      </TableCell>
      <TableCell className="max-w-48 truncate text-muted-foreground">
        {service.requirements.length === 0
          ? '—'
          : service.requirements.map(requirementLabel).join(', ')}
      </TableCell>
      <TableCell>
        {service.active ? (
          <span className="text-muted-foreground">Active</span>
        ) : (
          <Badge variant="secondary">Inactive</Badge>
        )}
      </TableCell>
      <TableCell className="text-right">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="sm" aria-label={`Actions for ${service.name}`}>
              <MoreHorizontal aria-hidden />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-40">
            <DropdownMenuItem onSelect={onEdit}>
              <Pencil aria-hidden />
              Edit
            </DropdownMenuItem>
            {service.active ? (
              <DropdownMenuItem
                variant="destructive"
                disabled={setActive.isPending}
                onSelect={() => {
                  if (
                    confirm(
                      `Deactivate ${service.name}?\n\n` +
                        'It drops off every booking screen until you restore it. ' +
                        'Appointments already booked against it are kept as they are.',
                    )
                  )
                    setActive.mutate(false)
                }}
              >
                <PowerOff aria-hidden />
                Deactivate
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem
                disabled={setActive.isPending}
                onSelect={() => setActive.mutate(true)}
              >
                <Power aria-hidden />
                Reactivate
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </TableCell>
    </TableRow>
  )
}

/** A requirement while it is being edited. `key` is React's, not the server's — a row with
 *  no resource chosen yet has nothing else to be identified by. */
type DraftRequirement = { key: number; kind: ResourceKind; resource_id: string | null }

/** Radix refuses an empty `SelectItem` value, and "any resource of this kind" needs one. A
 *  resource id is a UUID, so nothing can collide with this. */
const ANY = 'any'

let nextKey = 0

function toDraft(requirements: ServiceRequirement[]): DraftRequirement[] {
  return requirements.map((r) => ({ key: nextKey++, kind: r.kind, resource_id: r.resource_id }))
}

const sameStaff = (a: string[], b: string[]) =>
  a.length === b.length && [...a].sort().join() === [...b].sort().join()

const sameRequirements = (a: DraftRequirement[], b: ServiceRequirement[]) =>
  a.length === b.length &&
  a.every((r, i) => r.kind === b[i].kind && r.resource_id === b[i].resource_id)

/**
 * Create or edit, including both sets.
 *
 * The three endpoints are separate on the wire — the fields, who may deliver it, what it
 * needs — because each is replaced whole. Here they are one dialog and one Save, because
 * they are one decision; what is unchanged is simply not sent.
 */
function ServiceDialog(props: {
  service?: ServiceRow
  staff: StaffRow[]
  resources: ResourceRow[]
  palette: StaffColour[]
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const existing = props.service
  const [name, setName] = useState(existing?.name ?? '')
  const [description, setDescription] = useState(existing?.description ?? '')
  const [duration, setDuration] = useState(String(existing?.duration_minutes ?? 60))
  const [before, setBefore] = useState(String(existing?.buffer_before_minutes ?? 0))
  const [after, setAfter] = useState(String(existing?.buffer_after_minutes ?? 0))
  const [price, setPrice] = useState(centsToDollars(existing?.price_cents ?? 0))
  const [online, setOnline] = useState(existing?.bookable_online ?? true)
  const [staffIds, setStaffIds] = useState<string[]>(existing?.staff_ids ?? [])
  const [requirements, setRequirements] = useState<DraftRequirement[]>(
    toDraft(existing?.requirements ?? []),
  )

  // Only active people and only active resources are offered: assigning work to somebody
  // who has left, or requiring a room that is gone, is refused by the server anyway — and a
  // picker that offers what will be refused is the worst version of that conversation.
  const eligible = props.staff.filter((s) => s.active)
  const available = props.resources.filter((r) => r.active)

  const cents = dollarsToCents(price)
  const minutes = (value: string) => {
    const parsed = Number(value)
    return Number.isInteger(parsed) && parsed >= 0 && parsed % 5 === 0 ? parsed : null
  }
  const durationMinutes = minutes(duration)
  const beforeMinutes = minutes(before)
  const afterMinutes = minutes(after)

  const problems = {
    duration:
      durationMinutes === null || durationMinutes < 5
        ? 'At least 5 minutes, in 5-minute steps.'
        : undefined,
    buffers:
      beforeMinutes === null || afterMinutes === null ? 'In 5-minute steps.' : undefined,
    price: cents === null ? 'A dollar amount, and never less than nothing.' : undefined,
  }
  const incomplete = !name.trim() || Object.values(problems).some(Boolean)

  const save = useMutation({
    mutationFn: async () => {
      const draft: ServiceDraft = {
        name: name.trim(),
        description: description.trim() || null,
        duration_minutes: durationMinutes as number,
        buffer_before_minutes: beforeMinutes as number,
        buffer_after_minutes: afterMinutes as number,
        price_cents: cents as number,
        bookable_online: online,
        sort_order: existing?.sort_order ?? 0,
      }
      let saved = existing ? await updateService(existing.id, draft) : await createService(draft)
      if (!sameStaff(staffIds, existing?.staff_ids ?? [])) {
        saved = await replaceServiceStaff(saved.id, staffIds)
      }
      if (!sameRequirements(requirements, existing?.requirements ?? [])) {
        saved = await replaceServiceRequirements(
          saved.id,
          requirements.map(({ kind, resource_id }) => ({ kind, resource_id })),
        )
      }
      return saved
    },
    onSuccess: (service) => {
      toast.success(existing ? `Saved ${service.name}` : `Added ${service.name}`)
      queryClient.invalidateQueries({ queryKey: SERVICES })
      props.onClose()
    },
  })

  // A duplicate name is a 409 naming no field on the wire, but it is a fact about the name
  // the caller just typed — so it reads under that field rather than as a form-wide error.
  const nameConflict = save.error instanceof ApiError && save.error.status === 409

  const setRequirement = (key: number, patch: Partial<DraftRequirement>) =>
    setRequirements((rows) => rows.map((r) => (r.key === key ? { ...r, ...patch } : r)))

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{existing ? `Edit ${existing.name}` : 'Add service'}</DialogTitle>
          <DialogDescription>
            How long it takes, what it costs, who may deliver it, and what it needs.
          </DialogDescription>
        </DialogHeader>

        <Form onSubmit={() => !incomplete && save.mutate()}>
          <Field
            label="Name"
            htmlFor="service-name"
            error={nameConflict ? save.error?.message : undefined}
          >
            <Input
              id="service-name"
              required
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>

          <Field label="Description" htmlFor="service-description">
            <Textarea
              id="service-description"
              maxLength={2000}
              rows={2}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>

          <div className="grid grid-cols-3 gap-4">
            <Field
              label="Duration"
              htmlFor="service-duration"
              error={problems.duration}
              hint="Minutes"
            >
              <Input
                id="service-duration"
                type="number"
                min={5}
                step={5}
                value={duration}
                onChange={(e) => setDuration(e.target.value)}
              />
            </Field>
            <Field
              label="Buffer before"
              htmlFor="service-buffer-before"
              error={problems.buffers}
              hint="Minutes"
            >
              <Input
                id="service-buffer-before"
                type="number"
                min={0}
                step={5}
                value={before}
                onChange={(e) => setBefore(e.target.value)}
              />
            </Field>
            <Field label="Buffer after" htmlFor="service-buffer-after" hint="Minutes">
              <Input
                id="service-buffer-after"
                type="number"
                min={0}
                step={5}
                value={after}
                onChange={(e) => setAfter(e.target.value)}
              />
            </Field>
          </div>

          <Field
            label="Price"
            htmlFor="service-price"
            error={problems.price}
            hint="In dollars, before tax."
          >
            <Input
              id="service-price"
              inputMode="decimal"
              className="w-32 tabular-nums"
              value={price}
              onChange={(e) => setPrice(e.target.value)}
            />
          </Field>

          <div className="flex items-start gap-2">
            <Checkbox
              id="service-online"
              checked={online}
              onCheckedChange={(on) => setOnline(on === true)}
            />
            <Label htmlFor="service-online" className="font-normal">
              Bookable online
              <span className="block text-xs text-muted-foreground">
                Off means staff can book it but the client-facing portal never offers it.
              </span>
            </Label>
          </div>

          <fieldset className="flex flex-col gap-2">
            <legend className="mb-2 text-sm font-medium">Who may deliver this</legend>
            {eligible.length === 0 ? (
              <p className="text-xs text-muted-foreground">No active staff yet.</p>
            ) : (
              <div className="flex flex-col gap-2">
                {eligible.map((member) => (
                  <div key={member.id} className="flex items-center gap-2">
                    <Checkbox
                      id={`service-staff-${member.id}`}
                      checked={staffIds.includes(member.id)}
                      onCheckedChange={(on) =>
                        setStaffIds((ids) =>
                          on === true ? [...ids, member.id] : ids.filter((i) => i !== member.id),
                        )
                      }
                    />
                    <Label
                      htmlFor={`service-staff-${member.id}`}
                      className="flex items-center gap-2 font-normal"
                    >
                      <span
                        aria-hidden
                        className="size-3 shrink-0 rounded-full ring-1 ring-foreground/10"
                        style={{
                          backgroundColor:
                            props.palette.find((c) => c.key === member.colour)?.hex ??
                            'transparent',
                        }}
                      />
                      {member.display_name}
                    </Label>
                  </div>
                ))}
              </div>
            )}
          </fieldset>

          <fieldset className="flex flex-col gap-2">
            <legend className="mb-2 text-sm font-medium">What it needs</legend>
            <p className="text-xs text-muted-foreground">
              A room, a device, both, or neither. “Any” means whichever one is free — a named
              one means that exact room or device, and nothing else will do.
            </p>
            {requirements.map((row, index) => (
              <div key={row.key} className="flex items-center gap-2">
                <Select
                  value={row.kind}
                  onValueChange={(kind) =>
                    // The resource goes with the kind. Keeping a laser selected while the
                    // row now says "space" would be a request the server refuses, built by
                    // a screen that let it be built.
                    setRequirement(row.key, { kind: kind as ResourceKind, resource_id: null })
                  }
                >
                  <SelectTrigger className="w-36" aria-label={`Requirement ${index + 1} kind`}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="space">Space</SelectItem>
                    <SelectItem value="equipment">Equipment</SelectItem>
                  </SelectContent>
                </Select>
                <Select
                  value={row.resource_id ?? ANY}
                  onValueChange={(value) =>
                    setRequirement(row.key, { resource_id: value === ANY ? null : value })
                  }
                >
                  <SelectTrigger
                    className="flex-1"
                    aria-label={`Requirement ${index + 1} resource`}
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ANY}>
                      {row.kind === 'space' ? 'Any space' : 'Any equipment'}
                    </SelectItem>
                    {available
                      .filter((r) => r.kind === row.kind)
                      .map((r) => (
                        <SelectItem key={r.id} value={r.id}>
                          {r.name}
                        </SelectItem>
                      ))}
                  </SelectContent>
                </Select>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  aria-label={`Remove requirement ${index + 1}`}
                  onClick={() =>
                    setRequirements((rows) => rows.filter((r) => r.key !== row.key))
                  }
                >
                  <X aria-hidden />
                </Button>
              </div>
            ))}
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="self-start"
              onClick={() =>
                setRequirements((rows) => [
                  ...rows,
                  { key: nextKey++, kind: 'space', resource_id: null },
                ])
              }
            >
              <Plus aria-hidden />
              Add requirement
            </Button>
          </fieldset>

          {save.error && !nameConflict && <FormError>{save.error.message}</FormError>}

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={props.onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || incomplete}>
              {save.isPending ? 'Saving…' : existing ? 'Save service' : 'Add service'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
