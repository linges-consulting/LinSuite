import { SignaturePad } from '@/components/signature-pad'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { YES_NO, type FormField } from '@/lib/forms'

/**
 * One field of a form, as a client sees it. Shared by the builder's preview (Settings →
 * Forms) and the public form page (`/f/#<token>`), so what staff preview is what the client
 * gets. Visibility (`show_if`) is the caller's, through `lib/forms.ts`'s `visibleKeys`.
 *
 * `error` is a sentence shown under the field and tied to its control with
 * `aria-describedby`, so a screen reader hears it with the field.
 */
export function FormFieldInput(props: {
  field: FormField
  value: unknown
  onChange: (value: unknown) => void
  error?: string
  /** Signature fields only: told whenever a drawn or typed signature does not clear the
   *  server's ink thresholds, so the page holding a Submit button can hold it back. */
  onSignatureWeakChange?: (weak: boolean) => void
}) {
  const { field, value, onChange, error } = props
  const id = `field-${field.key}`
  const errorId = error ? `${id}-error` : undefined
  const problem = error && (
    <p id={errorId} className="text-sm text-destructive">
      {error}
    </p>
  )
  const label = (
    <>
      {field.label ? <span>{field.label}</span> : <span className="text-muted-foreground">(no label yet)</span>}
      {field.required && (
        <span className="text-destructive" aria-label="required">
          {' *'}
        </span>
      )}
    </>
  )
  const help = field.help && <p className="text-xs text-muted-foreground">{field.help}</p>
  const choices = (type: 'radio' | 'checkbox', options: readonly string[], text = (o: string) => o) => (
    <fieldset className="flex flex-col gap-2" aria-describedby={errorId}>
      <legend className="mb-1 text-sm font-medium">{label}</legend>
      {help}
      {options.map((o) => {
        const selected = type === 'radio' ? value === o : Array.isArray(value) && value.includes(o)
        return (
          <label key={o} className="flex items-center gap-2 text-sm">
            <input
              type={type}
              name={id}
              className="size-4 accent-primary"
              checked={selected}
              onChange={() =>
                onChange(
                  type === 'radio'
                    ? o
                    : selected
                      ? (value as string[]).filter((v) => v !== o)
                      : [...((value as string[]) ?? []), o],
                )
              }
            />
            {text(o)}
          </label>
        )
      })}
      {problem}
    </fieldset>
  )

  switch (field.type) {
    case 'heading':
      return <h4 className="text-base font-semibold">{field.label}</h4>
    case 'paragraph':
      return <p className="text-sm whitespace-pre-wrap">{field.label}</p>
    case 'yes_no':
      return choices('radio', YES_NO, (o) => (o === 'yes' ? 'Yes' : 'No'))
    case 'single_choice':
      return choices('radio', field.options ?? [])
    case 'multi_choice':
      return choices('checkbox', field.options ?? [])
    case 'acknowledgement':
      return (
        <div className="flex flex-col gap-2">
          <div className="flex items-start gap-2">
            <Checkbox
              id={id}
              className="mt-0.5"
              checked={value === true}
              aria-describedby={errorId}
              onCheckedChange={(on) => onChange(on === true)}
            />
            <Label htmlFor={id} className="font-normal whitespace-pre-wrap">
              {label}
            </Label>
          </div>
          {problem}
        </div>
      )
    case 'signature':
      return (
        <div className="flex flex-col gap-2">
          <span className="text-sm font-medium">{label}</span>
          {help}
          <SignaturePad
            id={id}
            value={value}
            onChange={onChange}
            describedBy={errorId}
            onWeakChange={props.onSignatureWeakChange}
          />
          {problem}
        </div>
      )
    default:
      return (
        <div className="flex flex-col gap-2">
          <Label htmlFor={id}>{label}</Label>
          {help}
          {field.type === 'long_text' ? (
            <Textarea
              id={id}
              value={(value as string) ?? ''}
              aria-describedby={errorId}
              aria-invalid={error ? true : undefined}
              onChange={(e) => onChange(e.target.value)}
            />
          ) : (
            <Input
              id={id}
              type={field.type === 'date' ? 'date' : 'text'}
              value={(value as string) ?? ''}
              aria-describedby={errorId}
              aria-invalid={error ? true : undefined}
              onChange={(e) => onChange(e.target.value)}
            />
          )}
          {problem}
        </div>
      )
  }
}
