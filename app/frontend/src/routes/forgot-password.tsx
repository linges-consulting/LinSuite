import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { AuthLayout, Field, Form, FormError } from '@/components/form'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { requestPasswordReset } from '@/lib/api'
import { useThrottle } from '@/lib/throttle'

/**
 * Ask for a reset link.
 *
 * The success message is deliberately hedged — "if an account exists" — because the server
 * answers identically for an address it has never seen. A screen that said "sent!" only for
 * real accounts would hand back the membership answer the backend just withheld, which is
 * the same rule the login screen follows for a wrong password.
 */
export function ForgotPasswordPage() {
  const [email, setEmail] = useState('')
  const submit = useMutation({ mutationFn: requestPasswordReset })
  // The address is limited to a few links per quarter of an hour, so this form cannot be
  // used to flood somebody's mailbox. Being refused says nothing about who has an account.
  const throttle = useThrottle(submit.error)

  return (
    <AuthLayout>
      <Card>
        <CardHeader className="border-b">
          <CardTitle>{submit.isSuccess ? 'Check your email' : 'Reset your password'}</CardTitle>
          <CardDescription>
            {submit.isSuccess
              ? `If an account exists for ${email}, a link to set a new password is on its way. It works once and expires within the hour.`
              : 'We will email you a link to set a new one.'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {submit.isSuccess ? (
            <Button asChild variant="outline" className="w-full">
              <Link to="/login">Back to sign in</Link>
            </Button>
          ) : (
            <Form onSubmit={() => submit.mutate(email)}>
              <Field
                label="Email"
                htmlFor="email"
                error={throttle.is429 ? undefined : submit.error?.message}
              >
                <Input
                  id="email"
                  type="email"
                  autoFocus
                  required
                  autoComplete="username"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  aria-invalid={submit.error ? true : undefined}
                />
              </Field>
              {throttle.message && <FormError>{throttle.message}</FormError>}
              <Button
                type="submit"
                className="w-full"
                disabled={submit.isPending || throttle.blocked}
              >
                {submit.isPending ? 'Sending…' : 'Send reset link'}
              </Button>
              <Link
                to="/login"
                className="text-center text-xs text-muted-foreground hover:text-foreground"
              >
                Back to sign in
              </Link>
            </Form>
          )}
        </CardContent>
      </Card>
    </AuthLayout>
  )
}
