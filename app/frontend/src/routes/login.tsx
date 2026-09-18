import { useState } from 'react'
import { AuthLayout, Field, Form } from '@/components/form'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { useLogin } from '@/lib/auth'

/**
 * Sign in. The server answers a wrong password and an unknown address identically, and this
 * screen shows exactly what it was told — guessing a friendlier message ("no such account")
 * would hand back the account-enumeration answer the backend just withheld.
 */
export function LoginPage() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const submit = useLogin()

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
            <Field label="Password" htmlFor="password" error={submit.error?.message}>
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
            <Button type="submit" className="w-full" disabled={submit.isPending}>
              {submit.isPending ? 'Signing in…' : 'Sign in'}
            </Button>
          </Form>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}
