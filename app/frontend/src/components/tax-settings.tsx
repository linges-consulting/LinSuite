import { useQuery } from '@tanstack/react-query'
import { ChoiceSelect } from '@/components/choice-select'
import { Field } from '@/components/form'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { fetchTaxComponents, type TaxConvention } from '@/lib/api'
import { TAX_COMPONENTS } from '@/lib/query-keys'

/**
 * A catalog item's own tax settings: which of the business's components apply ("GST yes,
 * PST no") and whether its price is entered before or including tax. Offers the components
 * this business's province picks up, plus any already selected.
 */
export function TaxSettingsFields(props: {
  idPrefix: string
  keys: string[]
  onKeysChange: (keys: string[]) => void
  convention: TaxConvention
  onConventionChange: (convention: TaxConvention) => void
}) {
  const components = useQuery({ queryKey: TAX_COMPONENTS, queryFn: fetchTaxComponents })
  const offered = (components.data ?? []).filter(
    (c) => (c.active && c.applicable_to_business) || props.keys.includes(c.code),
  )
  const toggle = (code: string, on: boolean) =>
    props.onKeysChange(on ? [...props.keys, code] : props.keys.filter((k) => k !== code))

  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-2 text-sm font-medium">Tax</legend>
      {offered.length === 0 ? (
        <p className="text-xs text-muted-foreground">No tax components are set up.</p>
      ) : (
        offered.map((c) => (
          <div key={c.id} className="flex items-center gap-2">
            <Checkbox
              id={`${props.idPrefix}-tax-${c.code}`}
              checked={props.keys.includes(c.code)}
              onCheckedChange={(on) => toggle(c.code, on === true)}
            />
            <Label htmlFor={`${props.idPrefix}-tax-${c.code}`} className="font-normal">
              {c.name} ({c.code})
            </Label>
          </div>
        ))
      )}
      <Field label="Price entered" htmlFor={`${props.idPrefix}-tax-convention`}>
        <ChoiceSelect
          id={`${props.idPrefix}-tax-convention`}
          value={props.convention}
          onValueChange={(value) => props.onConventionChange(value as TaxConvention)}
          options={[
            { value: 'exclusive', label: 'Before tax (tax added on top)' },
            { value: 'inclusive', label: 'Including tax' },
          ]}
        />
      </Field>
    </fieldset>
  )
}
