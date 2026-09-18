import { useMutation, useQuery } from '@tanstack/react-query'
import { Check, ChevronsUpDown, CircleCheck, KeyRound, Lock } from 'lucide-react'
import { useState } from 'react'
import { EmptyState } from '@/components/empty-state'
import { AuthLayout, Field, Form } from '@/components/form'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import {
  completeSetup,
  fetchSetupStatus,
  fetchTimezones,
  ApiError,
  type SetupPayload,
} from '@/lib/api'

const MIN_PASSWORD_LENGTH = 12 // matches core/security.py

type Step = 'token' | 'business' | 'admin' | 'done'

const STEPS = ['token', 'business', 'admin'] as const

const COPY: Record<(typeof STEPS)[number], { title: string; description: string }> = {
  token: {
    title: 'Unlock setup',
    description: 'This instance is unclaimed. Enter the setup token to continue.',
  },
  business: {
    title: 'Your business',
    description: 'The name and timezone everything else is scheduled against.',
  },
  admin: {
    title: 'Administrator account',
    description: `The first account. At least ${MIN_PASSWORD_LENGTH} characters, no other rules.`,
  },
}

/**
 * First-run wizard. The token comes from the container logs; nothing here works without it,
 * and the backend removes these routes the moment setup completes.
 */
export function SetupPage() {
  const { data: status } = useQuery({
    queryKey: ['setup-status'],
    queryFn: fetchSetupStatus,
    staleTime: Infinity,
  })
  const [step, setStep] = useState<Step>('token')
  const [form, setForm] = useState<SetupPayload>({
    token: '',
    business_name: '',
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    admin_email: '',
    admin_password: '',
  })
  const set = (patch: Partial<SetupPayload>) => setForm((f) => ({ ...f, ...patch }))
  // Lives here, not in the step, so stepping back and forward keeps both password fields.
  const [confirm, setConfirm] = useState('')

  const submit = useMutation({
    mutationFn: completeSetup,
    onSuccess: () => setStep('done'),
    // A bad token can only be found out here, at the end: send them back to fix it.
    onError: (error) => {
      if (error instanceof ApiError && error.status === 403) setStep('token')
    },
  })

  if (status && !status.required && step !== 'done') {
    return (
      <AuthLayout>
        <EmptyState
          icon={Lock}
          title="Setup is already complete"
          description="This instance has been claimed. The setup wizard is permanently disabled and cannot be re-entered."
          action={
            <Button asChild variant="outline">
              <a href="/">Go to the dashboard</a>
            </Button>
          }
        />
      </AuthLayout>
    )
  }

  if (step === 'done') {
    return (
      <AuthLayout>
        <Card>
          <CardHeader>
            <div className="mb-1 flex size-10 items-center justify-center rounded-lg bg-success/10 text-success">
              <CircleCheck className="size-5" aria-hidden />
            </div>
            <CardTitle>{form.business_name} is ready</CardTitle>
            <CardDescription>
              Your administrator account <strong className="font-medium">{form.admin_email}</strong>{' '}
              was created and the setup wizard has been permanently disabled.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {/* A full load, so the app starts from the post-setup state with no stale cache. */}
            <Button className="w-full" onClick={() => window.location.assign('/')}>
              Go to the dashboard
            </Button>
          </CardContent>
        </Card>
      </AuthLayout>
    )
  }

  const error = submit.error?.message

  return (
    <AuthLayout>
      <Card>
        <CardHeader className="border-b">
          <p className="text-xs font-medium text-muted-foreground">
            Step {STEPS.indexOf(step) + 1} of {STEPS.length}
          </p>
          <CardTitle>{COPY[step].title}</CardTitle>
          <CardDescription>{COPY[step].description}</CardDescription>
        </CardHeader>
        <CardContent>
          {step === 'token' && (
            <TokenStep
              value={form.token}
              error={error}
              onChange={(token) => set({ token })}
              onContinue={() => {
                submit.reset() // the rejected-token message dies with the token that caused it
                setStep('business')
              }}
            />
          )}
          {step === 'business' && (
            <BusinessStep
              form={form}
              onChange={set}
              onBack={() => setStep('token')}
              onContinue={() => setStep('admin')}
            />
          )}
          {step === 'admin' && (
            <AdminStep
              form={form}
              error={error}
              pending={submit.isPending}
              confirm={confirm}
              onConfirmChange={setConfirm}
              onChange={set}
              onBack={() => setStep('business')}
              onSubmit={() => submit.mutate(form)}
            />
          )}
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

function TokenStep(props: {
  value: string
  error?: string
  onChange: (value: string) => void
  onContinue: () => void
}) {
  return (
    <Form onSubmit={props.onContinue}>
      <Field label="Setup token" htmlFor="token" error={props.error}>
        <Input
          id="token"
          autoFocus
          required
          autoComplete="off"
          spellCheck={false}
          value={props.value}
          onChange={(e) => props.onChange(e.target.value)}
          aria-invalid={props.error ? true : undefined}
        />
      </Field>
      <p className="flex gap-2 text-xs text-muted-foreground">
        <KeyRound className="mt-px size-4 shrink-0" aria-hidden />
        <span>
          Printed once per start-up. Read it with <code className="font-mono">docker compose logs app</code>, or from the
          file at <code className="font-mono">SETUP_TOKEN_FILE</code> on the server.
        </span>
      </p>
      <Button type="submit" className="w-full">
        Continue
      </Button>
    </Form>
  )
}

function BusinessStep(props: {
  form: SetupPayload
  onChange: (patch: Partial<SetupPayload>) => void
  onBack: () => void
  onContinue: () => void
}) {
  const [error, setError] = useState<string>()

  return (
    <Form
      onSubmit={() => {
        if (!props.form.timezone) return setError('Select the timezone this business operates in.')
        props.onContinue()
      }}
    >
      <Field label="Business name" htmlFor="business-name">
        <Input
          id="business-name"
          autoFocus
          required
          maxLength={200}
          value={props.form.business_name}
          onChange={(e) => props.onChange({ business_name: e.target.value })}
        />
      </Field>
      <Field
        label="Timezone"
        htmlFor="timezone"
        error={error}
        hint="Opening hours and shifts are kept at their local wall-clock time in this zone."
      >
        <TimezoneCombobox
          value={props.form.timezone}
          onChange={(timezone) => {
            setError(undefined)
            props.onChange({ timezone })
          }}
        />
      </Field>
      <Actions onBack={props.onBack} label="Continue" />
    </Form>
  )
}

function AdminStep(props: {
  form: SetupPayload
  error?: string
  pending: boolean
  confirm: string
  onConfirmChange: (value: string) => void
  onChange: (patch: Partial<SetupPayload>) => void
  onBack: () => void
  onSubmit: () => void
}) {
  const confirm = props.confirm
  const [error, setError] = useState<{ password?: string; confirm?: string }>({})

  return (
    <Form
      onSubmit={() => {
        if (props.form.admin_password.length < MIN_PASSWORD_LENGTH)
          return setError({ password: `Use at least ${MIN_PASSWORD_LENGTH} characters.` })
        if (props.form.admin_password !== confirm)
          return setError({ confirm: 'The two passwords do not match.' })
        setError({})
        props.onSubmit()
      }}
    >
      <Field label="Email" htmlFor="admin-email">
        <Input
          id="admin-email"
          type="email"
          autoFocus
          required
          autoComplete="username"
          value={props.form.admin_email}
          onChange={(e) => props.onChange({ admin_email: e.target.value })}
        />
      </Field>
      <Field
        label="Password"
        htmlFor="admin-password"
        error={error.password}
        hint={`At least ${MIN_PASSWORD_LENGTH} characters. Length beats symbols — a short phrase works.`}
      >
        <Input
          id="admin-password"
          type="password"
          required
          autoComplete="new-password"
          value={props.form.admin_password}
          onChange={(e) => props.onChange({ admin_password: e.target.value })}
          aria-invalid={error.password ? true : undefined}
        />
      </Field>
      <Field
        label="Confirm password"
        htmlFor="admin-password-confirm"
        error={error.confirm ?? props.error}
      >
        <Input
          id="admin-password-confirm"
          type="password"
          required
          autoComplete="new-password"
          value={confirm}
          onChange={(e) => props.onConfirmChange(e.target.value)}
          aria-invalid={error.confirm ? true : undefined}
        />
      </Field>
      <Actions onBack={props.onBack} label={props.pending ? 'Creating…' : 'Create and finish'} pending={props.pending} />
    </Form>
  )
}

function TimezoneCombobox(props: { value: string; onChange: (value: string) => void }) {
  const [open, setOpen] = useState(false)
  const { data: zones = [] } = useQuery({
    queryKey: ['timezones'],
    queryFn: fetchTimezones,
    staleTime: Infinity,
  })

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          id="timezone"
          type="button"
          variant="outline"
          role="combobox"
          aria-expanded={open}
          className="w-full justify-between font-normal"
        >
          {props.value || 'Select a timezone'}
          <ChevronsUpDown className="opacity-50" aria-hidden />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-(--radix-popover-trigger-width) p-0">
        <Command>
          <CommandInput placeholder="Search timezones…" />
          <CommandList>
            <CommandEmpty>No matching timezone.</CommandEmpty>
            <CommandGroup>
              {zones.map((zone) => (
                <CommandItem
                  key={zone}
                  value={zone}
                  onSelect={() => {
                    props.onChange(zone)
                    setOpen(false)
                  }}
                >
                  {zone}
                  {zone === props.value && <Check className="ml-auto size-4" aria-hidden />}
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

function Actions(props: { onBack: () => void; label: string; pending?: boolean }) {
  return (
    <div className="flex gap-2">
      <Button type="button" variant="outline" onClick={props.onBack} disabled={props.pending}>
        Back
      </Button>
      <Button type="submit" className="flex-1" disabled={props.pending}>
        {props.label}
      </Button>
    </div>
  )
}
