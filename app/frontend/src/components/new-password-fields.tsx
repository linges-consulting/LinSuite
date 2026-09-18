import { Field } from '@/components/form'
import { Input } from '@/components/ui/input'
import { MIN_PASSWORD_LENGTH } from '@/lib/password'

/** The new-password pair, shared by the reset and change screens. */
export function NewPasswordFields(props: {
  password: string
  confirm: string
  onPassword: (value: string) => void
  onConfirm: (value: string) => void
  error?: string
  confirmError?: string
  autoFocus?: boolean
}) {
  return (
    <>
      <Field
        label="New password"
        htmlFor="new-password"
        error={props.error}
        hint={`At least ${MIN_PASSWORD_LENGTH} characters. Length beats symbols — a short phrase works.`}
      >
        <Input
          id="new-password"
          type="password"
          required
          autoFocus={props.autoFocus}
          autoComplete="new-password"
          value={props.password}
          onChange={(e) => props.onPassword(e.target.value)}
          aria-invalid={props.error ? true : undefined}
        />
      </Field>
      <Field label="Confirm new password" htmlFor="confirm-password" error={props.confirmError}>
        <Input
          id="confirm-password"
          type="password"
          required
          autoComplete="new-password"
          value={props.confirm}
          onChange={(e) => props.onConfirm(e.target.value)}
          aria-invalid={props.confirmError ? true : undefined}
        />
      </Field>
    </>
  )
}
