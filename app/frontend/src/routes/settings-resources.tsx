import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { MoreHorizontal, Pencil, Plus, Power, PowerOff, ShieldCheck } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
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
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import {
  ApiError,
  createResource,
  deactivateResource,
  fetchResources,
  fetchStaffPalette,
  reactivateResource,
  updateResource,
  type ResourceDraft,
  type ResourceKind,
  type ResourceRow,
  type StaffColour,
} from '@/lib/api'
import { invalidateScheduling } from '@/lib/query-client'
import { RESOURCES, STAFF_PALETTE } from '@/lib/query-keys'

/**
 * Settings → Resources: the spaces (treatment rooms, chairs, booths) and the equipment
 * (specialised machines, shared tools) a service is delivered in and with (PRD §1).
 *
 * Both are bookable, and both share every fact this screen edits — only `kind` tells them
 * apart, so one table component serves both tabs rather than two near-identical screens.
 *
 * This ticket only creates and manages the resource. The rule that neither can be claimed
 * by two appointments at once (tech-stack §15, §20 — a chair cannot be in two places) is
 * absolute and arrives with booking; nothing here enforces it.
 */
export function ResourcesPanel() {
  return (
    <Tabs defaultValue="space" className="gap-4">
      <TabsList variant="line">
        <TabsTrigger value="space">Spaces</TabsTrigger>
        <TabsTrigger value="equipment">Equipment</TabsTrigger>
      </TabsList>
      <TabsContent value="space">
        <ResourceTable kind="space" noun="space" />
      </TabsContent>
      <TabsContent value="equipment">
        <ResourceTable kind="equipment" noun="equipment" />
      </TabsContent>
    </Tabs>
  )
}

const COPY: Record<ResourceKind, { addLabel: string; blurb: string }> = {
  space: {
    addLabel: 'Add space',
    blurb: 'Treatment rooms, styling chairs, consultation booths, stations.',
  },
  equipment: {
    addLabel: 'Add equipment',
    blurb: 'Specialised machines, devices and shared tools.',
  },
}

function ResourceTable({ kind, noun }: { kind: ResourceKind; noun: string }) {
  const [includeInactive, setIncludeInactive] = useState(false)
  const resources = useQuery({
    queryKey: [...RESOURCES, kind, includeInactive],
    queryFn: () => fetchResources(kind, includeInactive),
    // Ticking the filter is a different query; without this the table flashes to a
    // skeleton for the length of one round trip, over a checkbox.
    placeholderData: (previous) => previous,
  })
  const palette = useQuery({ queryKey: STAFF_PALETTE, queryFn: fetchStaffPalette })
  const [editing, setEditing] = useState<ResourceRow | null>(null)
  const [creating, setCreating] = useState(false)

  if (resources.isPending || palette.isPending) {
    return <Skeleton className="h-64 w-full" />
  }
  if (resources.isError || palette.isError) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {(resources.error ?? palette.error)?.message}
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <p className="max-w-3xl text-sm text-muted-foreground">{COPY[kind].blurb}</p>
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <Checkbox
              id={`show-inactive-${kind}`}
              checked={includeInactive}
              onCheckedChange={(on) => setIncludeInactive(on === true)}
            />
            <Label htmlFor={`show-inactive-${kind}`} className="font-normal">
              Show inactive
            </Label>
          </div>
          <Button onClick={() => setCreating(true)}>
            <Plus aria-hidden />
            {COPY[kind].addLabel}
          </Button>
        </div>
      </div>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Name</TableHead>
            <TableHead>Description</TableHead>
            <TableHead>Status</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {resources.data?.length === 0 && (
            <TableRow>
              <TableCell colSpan={4} className="text-center text-muted-foreground">
                No {kind === 'space' ? 'spaces' : 'equipment'} yet.
              </TableCell>
            </TableRow>
          )}
          {resources.data?.map((resource) => (
            <ResourceLine
              key={resource.id}
              resource={resource}
              kind={kind}
              palette={palette.data ?? []}
              onEdit={() => setEditing(resource)}
            />
          ))}
        </TableBody>
      </Table>

      {creating && (
        <ResourceDialog
          kind={kind}
          noun={noun}
          palette={palette.data ?? []}
          onClose={() => setCreating(false)}
        />
      )}
      {editing && (
        <ResourceDialog
          kind={kind}
          noun={noun}
          resource={editing}
          palette={palette.data ?? []}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  )
}

/** The swatch a resource is painted with on the schedule, once it has one. Unlike staff,
 *  a resource need not carry a colour at all. */
function Swatch({ colour, palette }: { colour: string | null; palette: StaffColour[] }) {
  if (!colour) return <span className="text-muted-foreground">—</span>
  const found = palette.find((c) => c.key === colour)
  return (
    <>
      <span
        aria-hidden
        className="size-3 shrink-0 rounded-full ring-1 ring-foreground/10"
        style={{ backgroundColor: found?.hex ?? 'transparent' }}
      />
      <span className="sr-only">Colour: {found?.name ?? colour}</span>
    </>
  )
}

function ResourceLine(props: {
  resource: ResourceRow
  kind: ResourceKind
  palette: StaffColour[]
  onEdit: () => void
}) {
  const { resource } = props
  const queryClient = useQueryClient()
  const refresh = () => {
    // The bare prefix: the other tab's list and the service dialog's `[...RESOURCES, 'all']`
    // are the same rooms, and a room created here has to reach both.
    queryClient.invalidateQueries({ queryKey: RESOURCES })
    invalidateScheduling(queryClient)
  }

  const setActive = useMutation({
    mutationFn: (active: boolean) =>
      active ? reactivateResource(resource.id) : deactivateResource(resource.id),
    onSuccess: (updated) => {
      toast.success(
        updated.active ? `${updated.name} can be booked again` : `Deactivated ${updated.name}`,
        updated.active
          ? undefined
          : { description: 'It is kept for any appointment that already referenced it.' },
      )
      refresh()
    },
    onError: (error) => {
      toast.error(error.message)
      refresh()
    },
  })

  return (
    // Inactive rows are greyed rather than hidden when the filter is on — they are history,
    // and history that looks identical to the roster is the wrong kind of quiet.
    <TableRow className={resource.active ? undefined : 'opacity-55'}>
      <TableCell>
        <div className="flex items-center gap-2">
          <Swatch colour={resource.colour} palette={props.palette} />
          <span className="font-medium">{resource.name}</span>
        </div>
      </TableCell>
      <TableCell className="max-w-sm truncate text-muted-foreground">
        {resource.description ?? '—'}
      </TableCell>
      <TableCell>
        {resource.active ? (
          <span className="text-muted-foreground">Active</span>
        ) : (
          <Badge variant="secondary">Inactive</Badge>
        )}
      </TableCell>
      <TableCell className="text-right">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="sm" aria-label={`Actions for ${resource.name}`}>
              <MoreHorizontal aria-hidden />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-40">
            <DropdownMenuItem onSelect={props.onEdit}>
              <Pencil aria-hidden />
              Edit
            </DropdownMenuItem>
            {resource.active ? (
              <DropdownMenuItem
                variant="destructive"
                disabled={setActive.isPending}
                onSelect={() => {
                  if (
                    confirm(
                      `Deactivate ${resource.name}?\n\n` +
                        'It drops off every picker until you restore it. Appointments that ' +
                        'already reference it are kept.',
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

/**
 * Create or edit. `kind` is fixed by which tab it was opened from and never shown as a
 * field: it is chosen once, at creation, and this dialog is never the place that changes it.
 */
function ResourceDialog(props: {
  kind: ResourceKind
  noun: string
  resource?: ResourceRow
  palette: StaffColour[]
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const existing = props.resource
  const [name, setName] = useState(existing?.name ?? '')
  const [description, setDescription] = useState(existing?.description ?? '')
  const [colour, setColour] = useState(existing?.colour ?? '')
  const [sortOrder, setSortOrder] = useState(String(existing?.sort_order ?? 0))

  const draft: ResourceDraft = {
    name: name.trim(),
    description: description.trim() || null,
    colour: colour || null,
    sort_order: Number(sortOrder) || 0,
  }

  const save = useMutation({
    mutationFn: () =>
      existing ? updateResource(existing.id, draft) : createResource({ ...draft, kind: props.kind }),
    onSuccess: (resource) => {
      toast.success(existing ? `Saved ${resource.name}` : `Added ${resource.name}`)
      queryClient.invalidateQueries({ queryKey: RESOURCES })
      invalidateScheduling(queryClient)
      props.onClose()
    },
  })

  // A duplicate name is a 409 naming no field on the wire, but it is a fact about the name
  // the caller just typed — so it reads under that field rather than as a form-wide error.
  const nameConflict = save.error instanceof ApiError && save.error.status === 409
  const incomplete = !name.trim()

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {/* Reuses the trigger's own copy rather than "Add a ${noun}": "space" wants the
                article and "equipment" — uncountable — never does. */}
            {existing ? `Edit ${existing.name}` : COPY[props.kind].addLabel}
          </DialogTitle>
          <DialogDescription>
            {existing
              ? 'Its name, description, colour and place in the list.'
              : `Give this ${props.noun} a name — you can fill in the rest later.`}
          </DialogDescription>
        </DialogHeader>

        <Form onSubmit={() => !incomplete && save.mutate()}>
          <Field
            label="Name"
            htmlFor="resource-name"
            error={nameConflict ? save.error?.message : undefined}
          >
            <Input
              id="resource-name"
              required
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>

          <Field label="Description" htmlFor="resource-description">
            <Textarea
              id="resource-description"
              maxLength={2000}
              rows={3}
              value={description ?? ''}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>

          <Field
            label="Sort order"
            htmlFor="resource-sort"
            hint="Where it falls in the list. Lower shows first."
          >
            <Input
              id="resource-sort"
              type="number"
              step={1}
              className="w-24"
              value={sortOrder}
              onChange={(e) => setSortOrder(e.target.value)}
            />
          </Field>

          <fieldset className="flex flex-col gap-2">
            <legend className="mb-2 text-sm font-medium">Colour</legend>
            <p className="text-xs text-muted-foreground">
              Optional. Paints this {props.noun} on the schedule.
            </p>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                aria-label="None"
                aria-pressed={colour === ''}
                title="None"
                onClick={() => setColour('')}
                className={
                  'flex size-8 items-center justify-center rounded-lg border border-dashed ' +
                  'border-input text-muted-foreground ' +
                  (colour === '' ? 'ring-2 ring-foreground ring-offset-2 ring-offset-background' : '')
                }
              >
                —
              </button>
              {props.palette.map((c) => (
                <button
                  key={c.key}
                  type="button"
                  aria-label={c.name}
                  aria-pressed={colour === c.key}
                  title={c.name}
                  onClick={() => setColour(c.key)}
                  style={{ backgroundColor: c.hex, color: c.foreground }}
                  className={
                    'flex size-8 items-center justify-center rounded-lg ring-offset-2 ' +
                    'ring-offset-background transition-transform ' +
                    (colour === c.key ? 'ring-2 ring-foreground' : 'ring-1 ring-foreground/10')
                  }
                >
                  {colour === c.key && <ShieldCheck aria-hidden className="size-4" />}
                </button>
              ))}
            </div>
          </fieldset>

          {save.error && !nameConflict && <FormError>{save.error.message}</FormError>}

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={props.onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || incomplete}>
              {save.isPending ? 'Saving…' : existing ? `Save ${props.noun}` : `Add ${props.noun}`}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
