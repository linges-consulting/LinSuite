import type { FormEvent } from 'react'
import { BrandMark } from '@/components/brand-mark'
import { Label } from '@/components/ui/label'

/**
 * The centred, card-width page the app shows before there is an app: the setup wizard and
 * the login screen. Nothing here depends on a session, because neither of them has one.
 */
export function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-6 p-4">
      <BrandMark className="flex items-center gap-2.5 font-semibold" />
      <div className="w-full max-w-md">{children}</div>
    </div>
  )
}

/** A form whose submit handler never has to remember `preventDefault`. */
export function Form(props: { onSubmit: () => void; children: React.ReactNode }) {
  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    props.onSubmit()
  }
  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-5">
      {props.children}
    </form>
  )
}

/**
 * An error about the whole form rather than about one field — being throttled, or locked out.
 * It sits where the next thing to read is, immediately above the button it just disabled.
 */
export function FormError({ children }: { children: React.ReactNode }) {
  return (
    <p role="alert" className="text-sm text-destructive">
      {children}
    </p>
  )
}

/** Label above, control, then one line below it — the error if there is one, else the hint. */
export function Field(props: {
  label: string
  htmlFor: string
  error?: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={props.htmlFor}>{props.label}</Label>
      {props.children}
      {props.error ? (
        <p className="text-xs text-destructive">{props.error}</p>
      ) : props.hint ? (
        <p className="text-xs text-muted-foreground">{props.hint}</p>
      ) : null}
    </div>
  )
}
