import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, MoreHorizontal, Pencil, Plus, Power, PowerOff, TriangleAlert } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { Field, Form, FormError } from '@/components/form'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import {
  addTaxComponentRate,
  ApiError,
  confirmTax,
  createTaxComponent,
  fetchProvinces,
  fetchTaxComponents,
  fetchTaxStatus,
  updateTaxComponent,
  type TaxComponent,
  type TaxStatus,
} from '@/lib/api'
import { ONBOARDING, TAX_COMPONENTS, TAX_STATUS } from '@/lib/query-keys'

// Basis points, the `Staff.commission_rate_*_bp` convention (CLAUDE.md) — a screen thinks in
// percent, the wire thinks in basis points. Copied from `settings-staff.tsx` rather than
// shared: two lines, and the two screens have no other reason to import from one another.
const percent = (basisPoints: number) => Math.round(basisPoints) / 100
const basisPoints = (percentage: string) => Math.round(Number(percentage || 0) * 100)

/**
 * Settings → Billing → Tax: the components a catalog item can be taxed with (GST, PST, HST…)
 * and each one's effective-dated rate history (#57).
 *
 * This ticket only defines and prices components — which of them apply to a given catalog
 * item, and the resolved total on a bill, is a later ticket's screen. The one exception is
 * `applicable_to_business`: a badge here, read straight off the server, saying whether this
 * business's own province would pick a component up at all (acceptance criterion 1) — useful
 * context while defining components, not an enforcement of anything.
 */
export function TaxSettingsPanel() {
  const components = useQuery({ queryKey: TAX_COMPONENTS, queryFn: fetchTaxComponents })
  const provinces = useQuery({ queryKey: ['provinces'], queryFn: fetchProvinces, staleTime: Infinity })
  const status = useQuery({ queryKey: TAX_STATUS, queryFn: fetchTaxStatus })
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<TaxComponent | null>(null)
  const [ratingId, setRatingId] = useState<string | null>(null)

  if (components.isPending || provinces.isPending) return <Skeleton className="h-64 w-full" />
  if (components.isError || provinces.isError) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {(components.error ?? provinces.error)?.message}
      </p>
    )
  }

  const rating = ratingId ? components.data.find((c) => c.id === ratingId) : null

  return (
    <div className="flex flex-col gap-4">
      {status.data && <TaxPrefillBanner status={status.data} />}

      <div className="flex flex-wrap items-center justify-between gap-4">
        <p className="max-w-3xl text-sm text-muted-foreground">
          GST, HST, PST and the like, effective-dated so a rate change never rewrites a
          historical invoice. Each catalog item will later choose which of these apply to it.
        </p>
        <Button onClick={() => setCreating(true)}>
          <Plus aria-hidden />
          Add tax component
        </Button>
      </div>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Code</TableHead>
            <TableHead>Name</TableHead>
            <TableHead>Jurisdiction</TableHead>
            <TableHead>Current rate</TableHead>
            <TableHead>Status</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {components.data.length === 0 && (
            <TableRow>
              <TableCell colSpan={6} className="text-center text-muted-foreground">
                No tax components yet.
              </TableCell>
            </TableRow>
          )}
          {components.data.map((component) => (
            <TaxComponentLine
              key={component.id}
              component={component}
              provinceName={provinces.data.find((p) => p.code === component.province)?.name}
              onEdit={() => setEditing(component)}
              onRates={() => setRatingId(component.id)}
            />
          ))}
        </TableBody>
      </Table>

      {creating && (
        <TaxComponentDialog provinces={provinces.data} onClose={() => setCreating(false)} />
      )}
      {editing && (
        <TaxComponentDialog
          provinces={provinces.data}
          component={editing}
          onClose={() => setEditing(null)}
        />
      )}
      {rating && <TaxRateDialog component={rating} onClose={() => setRatingId(null)} />}
    </div>
  )
}

/** Tax pre-fill (#118): "Looks right" once, plus the province-change/newer-rate prompt.
 *  Neither ever changes a component on its own — a save through the table above, or nothing,
 *  is the only way a rate moves (spec #113: "nothing changes automatically"). */
function TaxPrefillBanner(props: { status: TaxStatus }) {
  const { status } = props
  const queryClient = useQueryClient()
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: TAX_STATUS })
    // Confirming can flip the onboarding checklist's tax step (#116).
    queryClient.invalidateQueries({ queryKey: ONBOARDING })
  }

  const confirm = useMutation({
    mutationFn: confirmTax,
    onSuccess: () => {
      toast.success('Tax setup confirmed')
      refresh()
    },
    onError: (error) => toast.error(error.message),
  })

  const needsConfirmation = status.prefilled && !status.confirmed_at
  const needsPrompt = status.province_changed || status.newer_rate_available

  if (!needsConfirmation && !needsPrompt) return null

  return (
    <div className="flex flex-col gap-3">
      {needsConfirmation && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-muted/40 p-4">
          <div className="flex items-start gap-2">
            <CheckCircle2 className="mt-0.5 size-4 text-muted-foreground" aria-hidden />
            <p className="text-sm text-muted-foreground">
              These tax components were filled in automatically from your business's province.
              Review them, then confirm.
            </p>
          </div>
          <Button size="sm" disabled={confirm.isPending} onClick={() => confirm.mutate()}>
            {confirm.isPending ? 'Confirming…' : 'Looks right'}
          </Button>
        </div>
      )}
      {needsPrompt && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-4">
          <TriangleAlert className="mt-0.5 size-4 text-amber-600" aria-hidden />
          <p className="text-sm">
            {status.province_changed
              ? "Your business's province has changed since these components were set up."
              : 'The official rate table has a newer rate than what is configured here.'}{' '}
            Nothing has changed automatically — review the components below and update them
            yourself if needed.
          </p>
        </div>
      )}
    </div>
  )
}

function TaxComponentLine(props: {
  component: TaxComponent
  provinceName?: string
  onEdit: () => void
  onRates: () => void
}) {
  const { component } = props
  const queryClient = useQueryClient()
  const refresh = () => queryClient.invalidateQueries({ queryKey: TAX_COMPONENTS })

  const setActive = useMutation({
    mutationFn: (active: boolean) => updateTaxComponent(component.id, { active }),
    onSuccess: (updated) => {
      toast.success(updated.active ? `${updated.name} is active again` : `Deactivated ${updated.name}`)
      refresh()
    },
    onError: (error) => {
      toast.error(error.message)
      refresh()
    },
  })

  return (
    <TableRow className={component.active ? undefined : 'opacity-55'}>
      <TableCell className="font-mono font-medium">{component.code}</TableCell>
      <TableCell>{component.name}</TableCell>
      <TableCell className="text-muted-foreground">
        {props.provinceName ?? 'Federal (all provinces)'}
        {!component.applicable_to_business && (
          <span className="ml-2 text-xs">— not this business's own jurisdiction</span>
        )}
      </TableCell>
      <TableCell>
        {component.current_rate_bp === null ? (
          <span className="text-muted-foreground">Not yet in effect</span>
        ) : (
          `${percent(component.current_rate_bp)}%`
        )}
      </TableCell>
      <TableCell>
        {component.active ? (
          <span className="text-muted-foreground">Active</span>
        ) : (
          <Badge variant="secondary">Inactive</Badge>
        )}
      </TableCell>
      <TableCell className="text-right">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="sm" aria-label={`Actions for ${component.name}`}>
              <MoreHorizontal aria-hidden />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-44">
            <DropdownMenuItem onSelect={props.onEdit}>
              <Pencil aria-hidden />
              Edit
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={props.onRates}>Rate history / new rate</DropdownMenuItem>
            {component.active ? (
              <DropdownMenuItem
                variant="destructive"
                disabled={setActive.isPending}
                onSelect={() => setActive.mutate(false)}
              >
                <PowerOff aria-hidden />
                Deactivate
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem disabled={setActive.isPending} onSelect={() => setActive.mutate(true)}>
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

/** Create or edit. `code` and the first rate are only asked for on create — a component
 *  without a rate could never actually tax a line, so the two are one request; editing never
 *  touches either (the module docstring — `code` is a stable key, a rate is added, not
 *  changed, through the separate dialog below). */
function TaxComponentDialog(props: {
  provinces: { code: string; name: string }[]
  component?: TaxComponent
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const existing = props.component
  const [code, setCode] = useState(existing?.code ?? '')
  const [name, setName] = useState(existing?.name ?? '')
  const [province, setProvince] = useState(existing?.province ?? '')
  const [rate, setRate] = useState('0')
  const [effectiveFrom, setEffectiveFrom] = useState(() => new Date().toISOString().slice(0, 10))

  const save = useMutation({
    mutationFn: () =>
      existing
        ? updateTaxComponent(existing.id, { name: name.trim(), province: province || null })
        : createTaxComponent({
            code: code.trim(),
            name: name.trim(),
            province: province || null,
            rate_bp: basisPoints(rate),
            effective_from: effectiveFrom,
          }),
    onSuccess: (component) => {
      toast.success(existing ? `Saved ${component.name}` : `Added ${component.name}`)
      queryClient.invalidateQueries({ queryKey: TAX_COMPONENTS })
      props.onClose()
    },
  })

  const codeConflict = save.error instanceof ApiError && save.error.status === 409
  const incomplete = !name.trim() || (!existing && !code.trim())

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{existing ? `Edit ${existing.name}` : 'Add tax component'}</DialogTitle>
          <DialogDescription>
            {existing
              ? 'Its name and jurisdiction. The code and its rates are not changed here.'
              : 'A code (GST, PST…), its jurisdiction, and its first rate.'}
          </DialogDescription>
        </DialogHeader>

        <Form onSubmit={() => !incomplete && save.mutate()}>
          {!existing && (
            <Field label="Code" htmlFor="tax-code" hint="Short and stable — GST, PST, HST, VAT.">
              <Input
                id="tax-code"
                required
                maxLength={16}
                value={code}
                onChange={(e) => setCode(e.target.value)}
              />
            </Field>
          )}

          <Field
            label="Name"
            htmlFor="tax-name"
            error={codeConflict ? save.error?.message : undefined}
          >
            <Input
              id="tax-name"
              required
              maxLength={100}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>

          <Field
            label="Jurisdiction"
            htmlFor="tax-province"
            hint="Federal applies regardless of the business's own province, like GST. A province restricts it to a business whose own address is in that province."
          >
            <Select value={province || 'federal'} onValueChange={(v) => setProvince(v === 'federal' ? '' : v)}>
              <SelectTrigger id="tax-province" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="federal">Federal (all provinces)</SelectItem>
                {props.provinces.map((p) => (
                  <SelectItem key={p.code} value={p.code}>
                    {p.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          {!existing && (
            <>
              <Field label="Rate" htmlFor="tax-rate" hint="Percent, e.g. 5 for 5%.">
                <Input
                  id="tax-rate"
                  type="number"
                  min={0}
                  max={100}
                  step={0.01}
                  className="w-32"
                  value={rate}
                  onChange={(e) => setRate(e.target.value)}
                />
              </Field>
              <Field label="Effective from" htmlFor="tax-effective-from">
                <Input
                  id="tax-effective-from"
                  type="date"
                  value={effectiveFrom}
                  onChange={(e) => setEffectiveFrom(e.target.value)}
                />
              </Field>
            </>
          )}

          {save.error && !codeConflict && <FormError>{save.error.message}</FormError>}

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={props.onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || incomplete}>
              {save.isPending ? 'Saving…' : existing ? 'Save' : 'Add component'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}

/** The rate history, and the form to open a new one. A new rate always closes whichever one
 *  was still open (the server's job, `billing/routes.py`) — this dialog never lets somebody
 *  pick an id to edit, only a future date to start a new rate from. */
function TaxRateDialog(props: { component: TaxComponent; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [rate, setRate] = useState('0')
  const [effectiveFrom, setEffectiveFrom] = useState(() => new Date().toISOString().slice(0, 10))

  const add = useMutation({
    mutationFn: () =>
      addTaxComponentRate(props.component.id, {
        rate_bp: basisPoints(rate),
        effective_from: effectiveFrom,
      }),
    onSuccess: (component) => {
      toast.success(`New rate added for ${component.name}`)
      queryClient.invalidateQueries({ queryKey: TAX_COMPONENTS })
    },
  })

  const rates = [...props.component.rates].sort((a, b) => (a.effective_from < b.effective_from ? -1 : 1))

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{props.component.name} — rate history</DialogTitle>
          <DialogDescription>
            A rate is never edited once it exists — opening a new one closes whichever was
            still in effect, so an already-issued invoice keeps reading the number it was
            issued under.
          </DialogDescription>
        </DialogHeader>

        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Rate</TableHead>
              <TableHead>From</TableHead>
              <TableHead>To</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rates.map((r) => (
              <TableRow key={r.id}>
                <TableCell>{percent(r.rate_bp)}%</TableCell>
                <TableCell>{r.effective_from}</TableCell>
                <TableCell>{r.effective_to ?? 'Current'}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        <Form onSubmit={() => add.mutate()}>
          <Field label="New rate" htmlFor="new-rate-value" hint="Percent, e.g. 5 for 5%.">
            <Input
              id="new-rate-value"
              type="number"
              min={0}
              max={100}
              step={0.01}
              className="w-32"
              value={rate}
              onChange={(e) => setRate(e.target.value)}
            />
          </Field>
          <Field label="Effective from" htmlFor="new-rate-from">
            <Input
              id="new-rate-from"
              type="date"
              value={effectiveFrom}
              onChange={(e) => setEffectiveFrom(e.target.value)}
            />
          </Field>

          {add.error && <FormError>{add.error.message}</FormError>}

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={props.onClose}>
              Close
            </Button>
            <Button type="submit" disabled={add.isPending}>
              {add.isPending ? 'Adding…' : 'Add new rate'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
