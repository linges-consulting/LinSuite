import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { toast } from 'sonner'
import { AuthLayout, Form, FormError } from '@/components/form'
import { CodeField, EMAIL_OTP_WARNING } from '@/components/mfa'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { requestEmailOtp } from '@/lib/api'
import { useLogout, useSession, useVerifyMfa } from '@/lib/auth'
import { useThrottle } from '@/lib/throttle'

/**
 * The second half of signing in: the whole application while `mfa.pending` is set.
 *
 * One field for all three kinds of code. The person typing knows whether they are holding an
 * authenticator, an emailed code or a piece of paper, and the server tells them apart — so
 * asking them to pick first would be a screen of radio buttons before a screen of one input.
 *
 * Sign out stays offered, for the same reason it does on the forced-change screen: somebody
 * whose phone is in another building must be able to leave a screen they cannot complete,
 * especially on a shared machine.
 */
export function MfaVerifyPage() {
  const [code, setCode] = useState('')
  const [emailed, setEmailed] = useState(false)
  const submit = useVerifyMfa()
  const signOut = useLogout()
  const { user } = useSession()
  // The same per-account counter a password feeds: a wrong code is a failed authentication,
  // and six digits guessed at without limit would be no second factor at all.
  const throttle = useThrottle(submit.error)

  const sendEmail = useMutation({
    mutationFn: requestEmailOtp,
    onSuccess: () => {
      setEmailed(true)
      toast.success('Code sent', { description: `Check ${user?.email ?? 'your inbox'}.` })
    },
    onError: (error) => toast.error(error.message),
  })

  return (
    <AuthLayout>
      <Card>
        <CardHeader className="border-b">
          <CardTitle>Enter your code</CardTitle>
          <CardDescription>
            {user?.mfa.method === 'email'
              ? 'We email a code to you each time you sign in.'
              : 'Open your authenticator app and enter the six-digit code for LinSuite.'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Form onSubmit={() => submit.mutate(code)}>
            <CodeField
              id="mfa-code"
              label="Code"
              autoFocus
              value={code}
              onChange={setCode}
              error={throttle.is429 ? undefined : submit.error?.message}
              hint="A recovery code works here too."
            />
            {throttle.message && <FormError>{throttle.message}</FormError>}
            <Button type="submit" className="w-full" disabled={submit.isPending || throttle.blocked}>
              {submit.isPending ? 'Checking…' : 'Continue'}
            </Button>

            {/* Offered whatever the policy says: the server refuses it where this account
                may not use one, and the refusal is honest. Hiding it would leave somebody
                whose codes are gone with no visible way forward at all. */}
            <div className="flex flex-col gap-2 border-t pt-4">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={sendEmail.isPending || emailed}
                onClick={() => sendEmail.mutate()}
              >
                {emailed ? 'Code sent — check your email' : 'Email me a code instead'}
              </Button>
              <p className="text-xs text-muted-foreground">{EMAIL_OTP_WARNING}</p>
            </div>

            <Button type="button" variant="ghost" className="w-full" onClick={() => signOut.mutate()}>
              Sign out
            </Button>
          </Form>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}
