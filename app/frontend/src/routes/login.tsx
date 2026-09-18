import { useState } from 'react'
import { Link } from 'react-router'
import { AuthLayout, Field, Form, FormError } from '@/components/form'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { useLogin } from '@/lib/auth'
import { useThrottle } from '@/lib/throttle'

/**
 * Sign in. The server answers a wrong password and an unknown address identically, and this
 * screen shows exactly what it was told — guessing a friendlier message ("no such account")
 * would hand back the account-enumeration answer the backend just withheld.
 *
 * A 429 is shown for what it is, with the wait counted down and the button disabled until it
 * runs out. Repeating "incorrect email or password" at somebody whose account is locked for
 * the next quarter of an hour would send them to retype a password that was never the problem.
 */
export function LoginPage() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const submit = useLogin()
  const throttle = useThrottle(submit.error)

  return (
    <AuthLayout>
      <Card>
        <CardHeader className="border-b">
          <CardTitle>Sign in</CardTitle>
          <CardDescription>Use the account your administrator set up for you.</CardDescription>
        </CardHeader>
        <CardContent>
          <Form onSubmit={() => submit.mutate({ email, password })}>
            <Field label="Email" htmlFor="email">
              <Input
                id="email"
                type="email"
                autoFocus
                required
                autoComplete="username"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </Field>
            <Field
              label="Password"
              htmlFor="password"
              error={throttle.blocked ? undefined : submit.error?.message}
            >
              <Input
                id="password"
                type="password"
                required
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                aria-invalid={submit.error ? true : undefined}
              />
            </Field>
            {throttle.message && <FormError>{throttle.message}</FormError>}
            <Button
              type="submit"
              className="w-full"
              disabled={submit.isPending || throttle.blocked}
            >
              {submit.isPending ? 'Signing in…' : 'Sign in'}
            </Button>
            <Link
              to="/forgot-password"
              className="text-center text-xs text-muted-foreground hover:text-foreground"
            >
              Forgot password?
            </Link>
          </Form>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}
