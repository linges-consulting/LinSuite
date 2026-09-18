import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import { AuthLayout, Form, FormError } from '@/components/form'
import { CodeField, EMAIL_OTP_WARNING, RecoveryCodes } from '@/components/mfa'
import { QrCode } from '@/components/qr-code'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  confirmEmailEnrolment,
  confirmEnrolment,
  startEmailEnrolment,
  startEnrolment,
} from '@/lib/api'
import { SESSION, useLogout, useSession } from '@/lib/auth'
import { MFA } from '@/lib/query-keys'

/**
 * Setting up a second factor: scan, confirm, keep the recovery codes.
 *
 * Three steps rather than one screen, because each is a different job and the middle one is
 * the one that matters — an account enrolled on the strength of a QR code nobody scanned
 * successfully is an account locked out of itself. The code is what makes it real.
 *
 * The same screen serves two arrivals: somebody who chose this from their own Security page,
 * and somebody the business's policy has left with nowhere else to go. The second is why
 * "Sign out" is offered and "Cancel" is not always — a gated session has nothing to go back
 * to, so offering Cancel would be a button that returns you to this screen.
 */
export function MfaEnrolPage({ gated = false }: { gated?: boolean }) {
  const { user } = useSession()
  const [codes, setCodes] = useState<string[] | null>(null)

  if (codes) return <EnrolmentDone codes={codes} gated={gated} />
  return (
    <AuthLayout>
      <Card>
        <CardHeader className="border-b">
          <CardTitle>Set up your second factor</CardTitle>
          <CardDescription>
            {gated
              ? 'This business requires a second factor on accounts that can administer it. Set one up to continue.'
              : 'A code from your phone, on top of your password.'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {user?.mfa.email_otp_allowed ? (
            <ChooseFactor onEnrolled={setCodes} gated={gated} />
          ) : (
            <TotpEnrolment onEnrolled={setCodes} gated={gated} />
          )}
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

/** Only shown where the business allows the weaker factor — otherwise there is no choice. */
function ChooseFactor(props: { onEnrolled: (codes: string[]) => void; gated: boolean }) {
  const [factor, setFactor] = useState<'totp' | 'email' | null>(null)

  if (factor === 'totp') return <TotpEnrolment {...props} />
  if (factor === 'email') return <EmailEnrolment {...props} />

  return (
    <div className="flex flex-col gap-4">
      <Button className="w-full" onClick={() => setFactor('totp')}>
        Use an authenticator app
      </Button>
      <div className="flex flex-col gap-2 border-t pt-4">
        <Button variant="outline" className="w-full" onClick={() => setFactor('email')}>
          Email me a code each time
        </Button>
        <p className="text-xs text-muted-foreground">{EMAIL_OTP_WARNING}</p>
      </div>
      <SignOut gated={props.gated} />
    </div>
  )
}

function TotpEnrolment(props: { onEnrolled: (codes: string[]) => void; gated: boolean }) {
  const [code, setCode] = useState('')
  const [manual, setManual] = useState(false)

  // Started on mount rather than behind a button: the first step has nothing to decide, and
  // a "Begin" click between arriving and seeing the QR code is ceremony.
  const enrolment = useQuery({ queryKey: [...MFA, 'enrolment'], queryFn: startEnrolment, retry: false, staleTime: Infinity })
  const confirm = useMutation({ mutationFn: confirmEnrolment, onSuccess: props.onEnrolled })

  if (enrolment.isPending) return <Skeleton className="h-64 w-full" />
  if (enrolment.isError) return <FormError>{enrolment.error.message}</FormError>

  return (
    <Form onSubmit={() => confirm.mutate(code)}>
      <div className="flex flex-col items-center gap-3">
        <QrCode value={enrolment.data.provisioning_uri} label="Scan this with your authenticator app" />
        {manual ? (
          <div className="w-full text-center">
            <p className="text-xs text-muted-foreground">Or type this key in by hand:</p>
            <p data-numeric className="break-all font-mono text-sm">
              {enrolment.data.secret}
            </p>
          </div>
        ) : (
          <Button type="button" variant="ghost" size="sm" onClick={() => setManual(true)}>
            Can't scan it?
          </Button>
        )}
      </div>
      <CodeField
        id="enrol-code"
        label="Code from the app"
        autoFocus
        value={code}
        onChange={setCode}
        error={confirm.error?.message}
        hint="Six digits, and they change every thirty seconds."
      />
      <Button type="submit" className="w-full" disabled={confirm.isPending}>
        {confirm.isPending ? 'Checking…' : 'Confirm'}
      </Button>
      <SignOut gated={props.gated} />
    </Form>
  )
}

function EmailEnrolment(props: { onEnrolled: (codes: string[]) => void; gated: boolean }) {
  const [code, setCode] = useState('')
  const { user } = useSession()
  const sent = useQuery({
    queryKey: [...MFA, 'email-enrolment'],
    queryFn: async () => {
      await startEmailEnrolment()
      return true
    },
    retry: false,
    staleTime: Infinity,
  })
  const confirm = useMutation({ mutationFn: confirmEmailEnrolment, onSuccess: props.onEnrolled })

  if (sent.isPending) return <Skeleton className="h-40 w-full" />
  if (sent.isError) return <FormError>{sent.error.message}</FormError>

  return (
    <Form onSubmit={() => confirm.mutate(code)}>
      <p className="text-sm text-muted-foreground">
        We sent a code to {user?.email}. Enter it to finish.
      </p>
      <CodeField
        id="enrol-code"
        label="Code from your email"
        autoFocus
        value={code}
        onChange={setCode}
        error={confirm.error?.message}
      />
      <Button type="submit" className="w-full" disabled={confirm.isPending}>
        {confirm.isPending ? 'Checking…' : 'Confirm'}
      </Button>
      <SignOut gated={props.gated} />
    </Form>
  )
}

/**
 * The codes, and the only time they exist anywhere but on paper. Leaving this screen is
 * deliberate and explicit — a redirect the moment enrolment succeeded would close the one
 * window in which they can be written down.
 */
function EnrolmentDone({ codes, gated }: { codes: string[]; gated: boolean }) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  return (
    <AuthLayout>
      <Card>
        <CardHeader className="border-b">
          <CardTitle>Save your recovery codes</CardTitle>
          <CardDescription>
            Your second factor is on. These codes are how you get in if you lose the device.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <RecoveryCodes codes={codes} />
          <Button
            className="w-full"
            onClick={async () => {
              // Re-read *before* navigating, not alongside it. `/me` has said the gate is
              // satisfied since the moment the code was confirmed, but this tab's cached
              // answer still says it is not — and routing reads the cache, so leaving on
              // the stale one would bounce straight back here and start the enrolment over.
              queryClient.invalidateQueries({ queryKey: MFA })
              await queryClient.refetchQueries({ queryKey: SESSION })
              navigate(gated ? '/' : '/security')
            }}
          >
            I have saved them
          </Button>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

function SignOut({ gated }: { gated: boolean }) {
  const signOut = useLogout()
  const navigate = useNavigate()
  return gated ? (
    <Button type="button" variant="ghost" className="w-full" onClick={() => signOut.mutate()}>
      Sign out
    </Button>
  ) : (
    <Button type="button" variant="ghost" className="w-full" onClick={() => navigate('/security')}>
      Cancel
    </Button>
  )
}
