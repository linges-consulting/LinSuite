import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Clock, Lightbulb } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { Field, Form } from '@/components/form'
import { TimezoneCombobox } from '@/components/timezone-combobox'
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
import { Label } from '@/components/ui/label'
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
  fetchAdminBusiness,
  fetchProvinces,
  updateBusiness,
  updateTimezone,
  type BusinessProfile,
} from '@/lib/api'
import { BRANDING_DOCUMENT, BUSINESS } from '@/lib/query-keys'

// An address, in the order it is written on an envelope. The province select is rendered
// between `city` and `postal_code` rather than being a text input in this list.
type ProfileField = { key: keyof BusinessProfile; label: string; hint?: string }

const BEFORE_PROVINCE: ProfileField[] = [
  { key: 'address_line1', label: 'Address' },
  { key: 'address_line2', label: 'Address line 2' },
  { key: 'city', label: 'City' },
]
const AFTER_PROVINCE: ProfileField[] = [
  { key: 'postal_code', label: 'Postal code', hint: 'Canadian format, like V8W 1P6.' },
  { key: 'phone', label: 'Phone' },
  { key: 'email', label: 'Email' },
  { key: 'gst_hst_number', label: 'GST / HST number' },
  { key: 'pst_qst_number', label: 'PST / QST number' },
]

/**
 * Who this business is, on documents and in the clock it keeps.
 *
 * The address is structured rather than a free-text block because a later task derives the
 * tax components from the province. The tax numbers are the opposite — free text, because
 * their formats differ per jurisdiction and a pattern that refused a valid one would be
 * unfixable from here.
 *
 * The timezone sits below the form and is saved by its own request. Two reasons it is not a
 * field on the profile: every recurring availability rule in the app is a wall-clock time
 * read against it, and a change is worth a sentence explaining that — which does not belong
 * beside a postal code somebody is fixing a typo in.
 */
export function BusinessPanel() {
  const queryClient = useQueryClient()
  const business = useQuery({ queryKey: BUSINESS, queryFn: fetchAdminBusiness })
  const [draft, setDraft] = useState<BusinessProfile | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})

  const save = useMutation({
    mutationFn: updateBusiness,
    onSuccess: (saved) => {
      queryClient.setQueryData(BUSINESS, saved)
      // The sidebar and the browser tab read the name from the public document.
      queryClient.invalidateQueries({ queryKey: BRANDING_DOCUMENT })
      setDraft(null)
      setErrors({})
      toast.success('Business details saved')
    },
    onError: (error) => {
      setErrors(fieldErrors(error))
      toast.error(error.message)
    },
  })

  if (business.isPending) return <Skeleton className="h-96 w-full" />
  if (business.isError) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {business.error.message}
      </p>
    )
  }

  const form = draft ?? business.data
  const set = (patch: Partial<BusinessProfile>) => setDraft({ ...form, ...patch })

  return (
    <div className="flex max-w-3xl flex-col gap-10">
      <Form onSubmit={() => save.mutate(strip(form))}>
        <Field label="Business name" htmlFor="business-name" error={errors.name}>
          <Input
            id="business-name"
            required
            maxLength={200}
            value={form.name}
            onChange={(e) => set({ name: e.target.value })}
          />
        </Field>

        {BEFORE_PROVINCE.map((field) => (
          <TextField key={field.key} field={field} form={form} errors={errors} set={set} />
        ))}

        <ProvinceField
          value={form.province}
          onChange={(province) => set({ province })}
          error={errors.province}
        />

        {AFTER_PROVINCE.map((field) => (
          <TextField key={field.key} field={field} form={form} errors={errors} set={set} />
        ))}

        <Field
          label="Currency symbol"
          htmlFor="currency_symbol"
          error={errors.currency_symbol}
          hint="Printed before every amount on invoices and receipts."
        >
          <Input
            id="currency_symbol"
            required
            maxLength={8}
            className="w-24"
            value={form.currency_symbol}
            onChange={(e) => set({ currency_symbol: e.target.value })}
          />
        </Field>

        <Field
          label="Receipt footer"
          htmlFor="receipt_footer"
          hint="Shown at the bottom of every invoice and receipt — registration wording, a thank you, a refund policy."
        >
          <Textarea
            id="receipt_footer"
            rows={3}
            value={form.receipt_footer ?? ''}
            onChange={(e) => set({ receipt_footer: e.target.value })}
          />
        </Field>

        <div className="grid grid-cols-2 gap-4">
          <Field
            label="Booking grid (minutes)"
            htmlFor="slot_granularity_minutes"
            error={errors.slot_granularity_minutes}
            hint="Appointments can start every this-many minutes, counted from midnight."
          >
            <Input
              id="slot_granularity_minutes"
              type="number"
              required
              min={5}
              max={60}
              step={5}
              value={form.slot_granularity_minutes}
              onChange={(e) => set({ slot_granularity_minutes: e.target.valueAsNumber })}
            />
          </Field>
          <Field
            label="Booking horizon (days)"
            htmlFor="booking_horizon_days"
            error={errors.booking_horizon_days}
            hint="How far ahead anything can be booked."
          >
            <Input
              id="booking_horizon_days"
              type="number"
              required
              min={1}
              max={365}
              value={form.booking_horizon_days}
              onChange={(e) => set({ booking_horizon_days: e.target.valueAsNumber })}
            />
          </Field>
        </div>

        <div className="flex gap-2">
          <Button type="submit" disabled={save.isPending || draft === null}>
            {save.isPending ? 'Saving…' : 'Save changes'}
          </Button>
          {draft !== null && (
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setDraft(null)
                setErrors({})
              }}
            >
              Discard
            </Button>
          )}
        </div>
      </Form>

      <TimezoneSection current={business.data.timezone} province={form.province} />
    </div>
  )
}

function TextField(props: {
  field: ProfileField
  form: BusinessProfile
  errors: Record<string, string>
  set: (patch: Partial<BusinessProfile>) => void
}) {
  const { key, label, hint } = props.field
  const error = props.errors[key]
  return (
    <Field label={label} htmlFor={key} error={error} hint={hint}>
      <Input
        id={key}
        value={(props.form[key] as string | null) ?? ''}
        onChange={(e) => props.set({ [key]: e.target.value } as Partial<BusinessProfile>)}
        aria-invalid={error ? true : undefined}
      />
    </Field>
  )
}

function ProvinceField(props: {
  value: string | null
  onChange: (value: string) => void
  error?: string
}) {
  const { data: provinces = [] } = useQuery({
    queryKey: ['provinces'],
    queryFn: fetchProvinces,
    staleTime: Infinity,
  })

  return (
    <Field label="Province or territory" htmlFor="province" error={props.error}>
      <Select value={props.value ?? ''} onValueChange={props.onChange}>
        <SelectTrigger id="province" className="w-full">
          <SelectValue placeholder="Select a province" />
        </SelectTrigger>
        <SelectContent>
          {provinces.map((province) => (
            <SelectItem key={province.code} value={province.code}>
              {province.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </Field>
  )
}

/**
 * The province suggests a zone; a person confirms it. Never applied on its own, because
 * Canadian provinces do not map onto timezones — Saskatchewan skips daylight saving, the BC
 * Kootenays are Mountain, northwestern Ontario is Central, and Labrador spans two zones.
 */
function TimezoneSection(props: { current: string; province: string | null }) {
  const queryClient = useQueryClient()
  const [chosen, setChosen] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const { data: provinces = [] } = useQuery({
    queryKey: ['provinces'],
    queryFn: fetchProvinces,
    staleTime: Infinity,
  })
  const suggestion = provinces.find((p) => p.code === props.province)

  const change = useMutation({
    mutationFn: updateTimezone,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: BUSINESS })
      setChosen(null)
      setConfirming(false)
      toast.success('Timezone changed')
    },
    onError: (error) => {
      setConfirming(false)
      toast.error(error.message)
    },
  })

  const value = chosen ?? props.current
  const changed = value !== props.current

  return (
    <section className="flex flex-col gap-4 border-t pt-8">
      <div className="flex flex-col gap-1">
        <h2 className="flex items-center gap-2 text-base font-medium">
          <Clock className="size-4 text-muted-foreground" aria-hidden />
          Timezone
        </h2>
        <p className="text-xs text-muted-foreground">
          Opening hours, shifts and cancellation windows are kept at their local wall-clock
          time in this zone.
        </p>
      </div>

      <div className="flex flex-col gap-2">
        <Label htmlFor="business-timezone">IANA timezone</Label>
        <TimezoneCombobox id="business-timezone" value={value} onChange={setChosen} />
      </div>

      {suggestion && suggestion.timezone !== value && (
        <p className="flex items-start gap-2 text-xs text-muted-foreground">
          <Lightbulb className="mt-px size-4 shrink-0" aria-hidden />
          <span>
            Most of {suggestion.name} keeps <strong className="font-medium">{suggestion.timezone}</strong>.{' '}
            <button
              type="button"
              className="text-primary underline underline-offset-2"
              onClick={() => setChosen(suggestion.timezone)}
            >
              Use it
            </button>{' '}
            — provinces do not map onto zones cleanly, so nothing is applied until you confirm.
          </span>
        </p>
      )}

      <div>
        <Button
          type="button"
          variant="outline"
          disabled={!changed || change.isPending}
          onClick={() => setConfirming(true)}
        >
          Change timezone
        </Button>
      </div>

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Change the business timezone?</DialogTitle>
            <DialogDescription asChild>
              <div className="flex flex-col gap-3 text-left">
                <p>
                  From <strong className="font-medium">{props.current}</strong> to{' '}
                  <strong className="font-medium">{value}</strong>.
                </p>
                <p>
                  Appointments already booked keep the exact moment they were booked for.
                  Recurring rules — weekly availability, shifts, cancellation windows — are
                  stored as local times and keep those local times, so a shift that starts at
                  9:00 AM will start at 9:00 AM in {value}.
                </p>
                <p>This is recorded in the audit log.</p>
              </div>
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
            <Button onClick={() => change.mutate(value)} disabled={change.isPending}>
              {change.isPending ? 'Changing…' : 'Change timezone'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  )
}

/** Only the profile fields go to the server; `country`, `timezone` and the rest are read-only. */
function strip(form: BusinessProfile): BusinessProfile {
  const {
    name,
    address_line1,
    address_line2,
    city,
    province,
    postal_code,
    phone,
    email,
    gst_hst_number,
    pst_qst_number,
    currency_symbol,
    receipt_footer,
    slot_granularity_minutes,
    booking_horizon_days,
  } = form
  return {
    name,
    address_line1,
    address_line2,
    city,
    province,
    postal_code,
    phone,
    email,
    gst_hst_number,
    pst_qst_number,
    currency_symbol,
    receipt_footer,
    slot_granularity_minutes,
    booking_horizon_days,
  }
}

/** FastAPI's 422 says which field it is unhappy about in `loc`; put the message under it. */
function fieldErrors(error: unknown): Record<string, string> {
  const detail = (error as { body?: { detail?: unknown } })?.body?.detail
  if (!Array.isArray(detail)) return {}
  return Object.fromEntries(
    detail
      .filter((entry) => Array.isArray(entry.loc) && entry.loc[0] === 'body')
      .map((entry) => [String(entry.loc[1]), entry.msg as string]),
  )
}
