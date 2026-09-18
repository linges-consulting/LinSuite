import { useState } from 'react'
import { AuthLayout, Field, Form } from '@/components/form'
import { NewPasswordFields } from '@/components/new-password-fields'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { ApiError } from '@/lib/api'
import { useChangePassword, useLogout, useSession } from '@/lib/auth'
import { localPasswordProblem } from '@/lib/password'

/**
 * The forced password change — the whole application while `must_change_password` is set.
 *
 * Routing sends every other path here and this route bounces to the shell the moment the
 * flag clears, so the session flag is the single authority on which screen is showing.
 * Succeeding is what clears it: the response rewrites the session cache and carries a fresh
 * cookie, so the tab that made the change is the one session that survives it.
 *
 * Sign out stays offered on purpose — an account that cannot leave a screen it cannot
 * complete is locked in, and this screen can be reached on a shared machine.
 */
export function ChangePasswordPage() {
  const [current, setCurrent] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [local, setLocal] = useState<{ error?: string; confirmError?: string }>({})
  const submit = useChangePassword()
  const signOut = useLogout()
  const { user } = useSession()

  // A 403 is the current password being wrong; anything else is about the new one.
  const currentError =
    submit.error instanceof ApiError && submit.error.status === 403
      ? submit.error.message
      : undefined

  return (
    <AuthLayout>
      <Card>
        <CardHeader className="border-b">
          <CardTitle>Set a new password</CardTitle>
          <CardDescription>
            Your account needs a new password before you can continue. Saving it signs out
            every other device.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Form
            onSubmit={() => {
              const problem = localPasswordProblem(password, confirm)
              setLocal(problem ?? {})
              if (!problem) submit.mutate({ current_password: current, new_password: password })
            }}
          >
            {/* Hidden, but present: a password manager cannot file a new password without
                knowing which account it belongs to, and Chrome warns about its absence. */}
            <input
              type="text"
              name="username"
              autoComplete="username"
              value={user?.email ?? ''}
              readOnly
              hidden
            />
            <Field label="Current password" htmlFor="current-password" error={currentError}>
              <Input
                id="current-password"
                type="password"
                autoFocus
                required
                autoComplete="current-password"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
                aria-invalid={currentError ? true : undefined}
              />
            </Field>
            <NewPasswordFields
              password={password}
              confirm={confirm}
              onPassword={setPassword}
              onConfirm={setConfirm}
              error={local.error ?? (currentError ? undefined : submit.error?.message)}
              confirmError={local.confirmError}
            />
            <Button type="submit" className="w-full" disabled={submit.isPending}>
              {submit.isPending ? 'Saving…' : 'Save new password'}
            </Button>
            <Button
              type="button"
              variant="ghost"
              className="w-full"
              onClick={() => signOut.mutate()}
            >
              Sign out
            </Button>
          </Form>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}
