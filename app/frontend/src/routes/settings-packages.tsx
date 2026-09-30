import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { MoreHorizontal, PackageOpen, Pencil, Plus, Power, PowerOff, X } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { EmptyState } from '@/components/empty-state'
import { TaxSettingsFields } from '@/components/tax-settings'
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
import { Textarea } from '@/components/ui/textarea'
import {
  ApiError,
  createPackageDefinition,
  deactivatePackageDefinition,
  fetchPackageDefinitions,
  fetchServices,
  reactivatePackageDefinition,
  replacePackageDefinitionServices,
  updatePackageDefinition,
  type PackageDefinitionDraft,
  type PackageDefinitionRow,
  type PackageDefinitionServiceDraft,
  type ServiceRow,
} from '@/lib/api'
import { useCan } from '@/lib/capability-gate'
import { centsToDollars, dollarsToCents } from '@/lib/money'
import { PACKAGE_DEFINITIONS, SERVICES } from '@/lib/query-keys'

/**
 * Settings → Packages (spec #95 user stories 57-59, #101): admin-defined bundles of services
 * sold as prepaid credit — create, edit, deactivate and reactivate, with per-item tax toggles
 * and a tax convention presented the same way Services and Products present theirs.
 *
 * Every route behind this screen (`billing/packages.py`) is `billing.manage`, an
 * administrative capability — refused server-side outside Admin Mode regardless of who holds
 * it. `useCan` is the one gate: an account without the capability, or holding it but not in
 * Admin Mode right now, never even asks the list endpoint (spec #95's "absent, never
 * disabled" rule) — and, per spec #113's Staff Mode section, sees nothing here at all rather
 * than an explanation of why (this whole screen sits behind `Settings`, which itself renders
 * nothing but the mode switcher's page outside Admin Mode — see `RequireAdminMode`).
 */
export function PackagesPanel() {
  const canManage = useCan('billing.manage')

  if (!canManage) return null

  return <PackagesTable />
}

function PackagesTable() {
  const [includeInactive, setIncludeInactive] = useState(false)
  const packages = useQuery({
    queryKey: [...PACKAGE_DEFINITIONS, includeInactive],
    queryFn: () => fetchPackageDefinitions(includeInactive),
    placeholderData: (previous) => previous,
  })
  // Inactive services included, deliberately — the same reason `ServicesPanel` fetches its
  // own staff list that way: a package already naming a service that has since been
  // deactivated has to be able to *say so*, not render as a blank nobody can act on.
  const services = useQuery({ queryKey: [...SERVICES, 'all'], queryFn: () => fetchServices(true) })
  const [editing, setEditing] = useState<PackageDefinitionRow | null>(null)
  const [creating, setCreating] = useState(false)

  const pending = packages.isPending || services.isPending
  const failed = packages.isError ? packages.error : services.isError ? services.error : null
  if (pending) return <Skeleton className="h-64 w-full" />
  if (failed) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {failed.message}
      </p>
    )
  }

  const eligibleServices = services.data ?? []

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <p className="max-w-3xl text-sm text-muted-foreground">
          Bundles of services sold as prepaid credit — a price, an optional expiry, and how
          many credits each included service carries.
        </p>
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <Checkbox
              id="show-inactive-packages"
              checked={includeInactive}
              onCheckedChange={(on) => setIncludeInactive(on === true)}
            />
            <Label htmlFor="show-inactive-packages" className="font-normal">
              Show inactive
            </Label>
          </div>
          {packages.data?.length !== 0 && (
            <Button onClick={() => setCreating(true)}>
              <Plus aria-hidden />
              Add package
            </Button>
          )}
        </div>
      </div>

      {packages.data?.length === 0 ? (
        <EmptyState
          icon={PackageOpen}
          title="No packages yet"
          description="Bundle services into prepaid credit that clients can buy and redeem over time."
          action={
            <Button onClick={() => setCreating(true)}>
              <Plus aria-hidden />
              Add package
            </Button>
          }
        />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead className="text-right">Price</TableHead>
              <TableHead>Expiry</TableHead>
              <TableHead>Transferable</TableHead>
              <TableHead>Services</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {packages.data?.map((row) => (
              <PackageLine key={row.id} row={row} onEdit={() => setEditing(row)} />
            ))}
          </TableBody>
        </Table>
      )}

      {creating && (
        <PackageDialog services={eligibleServices} onClose={() => setCreating(false)} />
      )}
      {editing && (
        <PackageDialog
          services={eligibleServices}
          definition={editing}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  )
}

function PackageLine({ row, onEdit }: { row: PackageDefinitionRow; onEdit: () => void }) {
  const queryClient = useQueryClient()
  const refresh = () => queryClient.invalidateQueries({ queryKey: PACKAGE_DEFINITIONS })

  const setActive = useMutation({
    mutationFn: (active: boolean) =>
      active ? reactivatePackageDefinition(row.id) : deactivatePackageDefinition(row.id),
    onSuccess: (updated) => {
      toast.success(
        updated.active ? `${updated.name} can be sold again` : `Deactivated ${updated.name}`,
        updated.active
          ? undefined
          : { description: 'Credits already sold against it are kept as they are.' },
      )
      refresh()
    },
    onError: (error) => {
      toast.error(error.message)
      refresh()
    },
  })

  return (
    <TableRow className={row.active ? undefined : 'opacity-55'}>
      <TableCell>
        <span className="font-medium">{row.name}</span>
        {row.description && (
          <span className="block max-w-xs truncate text-xs text-muted-foreground">
            {row.description}
          </span>
        )}
      </TableCell>
      <TableCell className="text-right tabular-nums">${centsToDollars(row.price_cents)}</TableCell>
      <TableCell className="tabular-nums text-muted-foreground">
        {row.expires_after_days ? `${row.expires_after_days} days` : 'No expiry'}
      </TableCell>
      <TableCell>{row.transferable ? 'Yes' : 'No'}</TableCell>
      <TableCell className="max-w-64 truncate text-muted-foreground">
        {row.services.map((s) => `${s.service_name} ×${s.credits}`).join(', ')}
      </TableCell>
      <TableCell>
        {row.active ? (
          <span className="text-muted-foreground">Active</span>
        ) : (
          <Badge variant="secondary">Inactive</Badge>
        )}
      </TableCell>
      <TableCell className="text-right">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="sm" aria-label={`Actions for ${row.name}`}>
              <MoreHorizontal aria-hidden />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-40">
            <DropdownMenuItem onSelect={onEdit}>
              <Pencil aria-hidden />
              Edit
            </DropdownMenuItem>
            {row.active ? (
              <DropdownMenuItem
                variant="destructive"
                disabled={setActive.isPending}
                onSelect={() => {
                  if (
                    confirm(
                      `Deactivate ${row.name}?\n\n` +
                        'It drops off the sellable list until you restore it. ' +
                        'Credits already sold against it are kept as they are.',
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

/** A package's services set while it is being edited. `lost` is a service that has since
 *  been deactivated — the server refuses a save that still names it, so this row is drawn
 *  read-only with a remove control rather than as a checkbox nobody can toggle, the same
 *  shape `ServiceDialog`'s departed-staff rows take. */
type DraftPackageService = { service_id: string; credits: number; lost?: { name: string } }

function toDraft(services: PackageDefinitionRow['services']): DraftPackageService[] {
  return services.map((s) => ({
    service_id: s.service_id,
    credits: s.credits,
    ...(s.service_active ? {} : { lost: { name: s.service_name } }),
  }))
}

const sameServices = (a: DraftPackageService[], b: PackageDefinitionRow['services']) =>
  a.length === b.length &&
  [...a].sort((x, y) => x.service_id.localeCompare(y.service_id)).every((row, i) => {
    const sorted = [...b].sort((x, y) => x.service_id.localeCompare(y.service_id))
    return row.service_id === sorted[i].service_id && row.credits === sorted[i].credits
  })

function PackageDialog(props: {
  services: ServiceRow[]
  definition?: PackageDefinitionRow
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const existing = props.definition
  const [name, setName] = useState(existing?.name ?? '')
  const [description, setDescription] = useState(existing?.description ?? '')
  const [price, setPrice] = useState(centsToDollars(existing?.price_cents ?? 0))
  const [expiresAfterDays, setExpiresAfterDays] = useState(
    existing?.expires_after_days ? String(existing.expires_after_days) : '',
  )
  const [transferable, setTransferable] = useState(existing?.transferable ?? false)
  const [taxKeys, setTaxKeys] = useState<string[]>(existing?.tax_component_keys ?? [])
  const [taxConvention, setTaxConvention] = useState(existing?.tax_convention ?? 'exclusive')
  const [services, setServices] = useState<DraftPackageService[]>(toDraft(existing?.services ?? []))

  const eligible = props.services.filter((s) => s.active)
  const lost = services.filter((row) => row.lost)

  const cents = dollarsToCents(price)
  const expiresValue = (() => {
    if (!expiresAfterDays.trim()) return null
    const parsed = Number(expiresAfterDays)
    return Number.isInteger(parsed) && parsed >= 1 && parsed <= 3650 ? parsed : undefined
  })()

  const problems = {
    price: cents === null ? 'A dollar amount, and never less than nothing.' : undefined,
    expiry: expiresValue === undefined ? '1–3650 days, or leave it blank for no expiry.' : undefined,
    services: services.length === 0 ? 'At least one service, with its credit count.' : undefined,
  }
  const incomplete =
    !name.trim() ||
    Object.values(problems).some(Boolean) ||
    services.some((row) => !Number.isInteger(row.credits) || row.credits < 1 || row.credits > 9999)

  const toggleService = (serviceId: string, on: boolean) =>
    setServices((rows) =>
      on ? [...rows, { service_id: serviceId, credits: 1 }] : rows.filter((r) => r.service_id !== serviceId),
    )
  const setCredits = (serviceId: string, credits: number) =>
    setServices((rows) => rows.map((r) => (r.service_id === serviceId ? { ...r, credits } : r)))
  const removeLost = (serviceId: string) =>
    setServices((rows) => rows.filter((r) => r.service_id !== serviceId))

  const save = useMutation({
    mutationFn: async () => {
      const draft: PackageDefinitionDraft = {
        name: name.trim(),
        description: description.trim() || null,
        price_cents: cents as number,
        expires_after_days: expiresValue ?? null,
        transferable,
        tax_component_keys: taxKeys,
        tax_convention: taxConvention,
      }
      const servicesPayload: PackageDefinitionServiceDraft[] = services.map((row) => ({
        service_id: row.service_id,
        credits: row.credits,
      }))
      if (!existing) return createPackageDefinition({ ...draft, services: servicesPayload })
      let saved = await updatePackageDefinition(existing.id, draft)
      if (!sameServices(services, existing.services)) {
        saved = await replacePackageDefinitionServices(saved.id, servicesPayload)
      }
      return saved
    },
    onSuccess: (definition) => {
      toast.success(existing ? `Saved ${definition.name}` : `Added ${definition.name}`)
      queryClient.invalidateQueries({ queryKey: PACKAGE_DEFINITIONS })
      props.onClose()
    },
  })

  const nameConflict = save.error instanceof ApiError && save.error.status === 409

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{existing ? `Edit ${existing.name}` : 'Add package'}</DialogTitle>
          <DialogDescription>
            What it costs, how long a purchase is good for, and the services it carries credits
            for.
          </DialogDescription>
        </DialogHeader>

        <Form onSubmit={() => !incomplete && save.mutate()}>
          <Field
            label="Name"
            htmlFor="package-name"
            error={nameConflict ? save.error?.message : undefined}
          >
            <Input
              id="package-name"
              required
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>

          <Field label="Description" htmlFor="package-description">
            <Textarea
              id="package-description"
              maxLength={2000}
              rows={2}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>

          <div className="grid grid-cols-2 gap-4">
            <Field
              label="Price"
              htmlFor="package-price"
              error={problems.price}
              hint={
                taxConvention === 'inclusive'
                  ? 'In dollars, including tax.'
                  : 'In dollars, before tax.'
              }
            >
              <Input
                id="package-price"
                inputMode="decimal"
                className="tabular-nums"
                value={price}
                onChange={(e) => setPrice(e.target.value)}
              />
            </Field>
            <Field
              label="Expires after"
              htmlFor="package-expiry"
              error={problems.expiry}
              hint="Days from purchase, or blank for no expiry"
            >
              <Input
                id="package-expiry"
                type="number"
                min={1}
                max={3650}
                value={expiresAfterDays}
                onChange={(e) => setExpiresAfterDays(e.target.value)}
              />
            </Field>
          </div>

          <TaxSettingsFields
            idPrefix="package"
            keys={taxKeys}
            onKeysChange={setTaxKeys}
            convention={taxConvention}
            onConventionChange={setTaxConvention}
          />

          <div className="flex items-start gap-2">
            <Checkbox
              id="package-transferable"
              checked={transferable}
              onCheckedChange={(on) => setTransferable(on === true)}
            />
            <Label htmlFor="package-transferable" className="font-normal">
              Transferable
              <span className="block text-xs text-muted-foreground">
                Whether a purchase can be moved to another client.
              </span>
            </Label>
          </div>

          <fieldset className="flex flex-col gap-2">
            <legend className="mb-2 text-sm font-medium">Services and credits</legend>
            {problems.services && <p className="text-xs text-destructive">{problems.services}</p>}
            {eligible.length === 0 ? (
              <p className="text-xs text-muted-foreground">No active services yet.</p>
            ) : (
              <div className="flex flex-col gap-2">
                {eligible.map((service) => {
                  const row = services.find((r) => r.service_id === service.id && !r.lost)
                  return (
                    <div key={service.id} className="flex items-center gap-2">
                      <Checkbox
                        id={`package-service-${service.id}`}
                        checked={row !== undefined}
                        onCheckedChange={(on) => toggleService(service.id, on === true)}
                      />
                      <Label
                        htmlFor={`package-service-${service.id}`}
                        className="flex-1 font-normal"
                      >
                        {service.name}
                      </Label>
                      {row && (
                        <Input
                          type="number"
                          min={1}
                          max={9999}
                          className="w-20 tabular-nums"
                          aria-label={`${service.name} credits`}
                          value={row.credits}
                          onChange={(e) => setCredits(service.id, Number(e.target.value))}
                        />
                      )}
                    </div>
                  )
                })}
              </div>
            )}
            {lost.map((row) => (
              <div key={row.service_id} className="flex items-center gap-2 text-muted-foreground">
                <Checkbox checked disabled aria-label={row.lost!.name} />
                <span className="flex-1 text-sm">
                  {row.lost!.name} <span className="text-xs">— deactivated</span> ×{row.credits}
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  aria-label={`Remove ${row.lost!.name}`}
                  onClick={() => removeLost(row.service_id)}
                >
                  <X aria-hidden />
                </Button>
              </div>
            ))}
            {lost.length > 0 && (
              <p className="text-xs text-muted-foreground">
                A service this package includes has been deactivated. Remove it or reactivate
                the service before saving changes to what's included.
              </p>
            )}
          </fieldset>

          {save.error && !nameConflict && <FormError>{save.error.message}</FormError>}

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={props.onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || incomplete}>
              {save.isPending ? 'Saving…' : existing ? 'Save package' : 'Add package'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
