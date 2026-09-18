import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { AuthLayout, Form } from '@/components/form'
import { NewPasswordFields } from '@/components/new-password-fields'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ApiError, confirmPasswordReset } from '@/lib/api'
import { localPasswordProblem } from '@/lib/password'

/**
 * Spend a reset link.
 *
 * A 400 here means the link expired or was already used, and it is a dead end — the token in
 * the URL will never work again, so the screen offers the only useful next step rather than
 * letting someone retype a password against a link that cannot accept one.
 */
export function ResetPasswordPage() {
  const [params] = useSearchParams()
  const token = params.get('token') ?? ''
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [local, setLocal] = useState<{ error?: string; confirmError?: string }>({})
  const submit = useMutation({ mutationFn: confirmPasswordReset })

  const spent = !token || (submit.error instanceof ApiError && submit.error.status === 400)
  if (spent) return <DeadLink />

  if (submit.isSuccess) {
    return (
      <Shell title="Password updated" description="Every other session has been signed out.">
        <Button asChild className="w-full">
          <Link to="/login">Sign in</Link>
        </Button>
      </Shell>
    )
  }

  return (
    <Shell title="Choose a new password" description="This link works once.">
      <Form
        onSubmit={() => {
          const problem = localPasswordProblem(password, confirm)
          setLocal(problem ?? {})
          if (!problem) submit.mutate({ token, new_password: password })
        }}
      >
        <NewPasswordFields
          autoFocus
          password={password}
          confirm={confirm}
          onPassword={setPassword}
          onConfirm={setConfirm}
          error={local.error ?? submit.error?.message}
          confirmError={local.confirmError}
        />
        <Button type="submit" className="w-full" disabled={submit.isPending}>
          {submit.isPending ? 'Saving…' : 'Set new password'}
        </Button>
      </Form>
    </Shell>
  )
}

function DeadLink() {
  return (
    <Shell
      title="This link has expired"
      description="Reset links work once and last under an hour. Ask for a new one."
    >
      <Button asChild className="w-full">
        <Link to="/forgot-password">Request another link</Link>
      </Button>
    </Shell>
  )
}

function Shell(props: { title: string; description: string; children: React.ReactNode }) {
  return (
    <AuthLayout>
      <Card>
        <CardHeader className="border-b">
          <CardTitle>{props.title}</CardTitle>
          <CardDescription>{props.description}</CardDescription>
        </CardHeader>
        <CardContent>{props.children}</CardContent>
      </Card>
    </AuthLayout>
  )
}
