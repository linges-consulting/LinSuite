import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, Navigate } from 'react-router'
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
  // A current code, when there is a factor being replaced. The server requires one — this is
  // the one thing behind the second factor that was not itself protected by it — so it is
  // collected before the QR code rather than discovered as a 403 on a screen already drawn.
  const [current, setCurrent] = useState<string | null>(null)
  // Locked in at mount, deliberately not read live off `gated` again below: confirming the
  // enrolment is exactly what flips the server's `enrolment_required` answer to false, and
  // `EnrolmentDone`'s own re-read of the session (so it can pick up an Admin Mode grant the
  // server opened alongside it, #117) lands *before* its `setLeft(true)` — which would hand
  // this component a fresh, now-ungated `gated` prop a render ahead of the navigate it drives,
  // and send a forced enrolment to Security instead of Home.
  const [wasGated] = useState(gated)

  if (codes) return <EnrolmentDone codes={codes} gated={wasGated} />
  if (user?.mfa.enrolled && current === null) return <ConfirmCurrent onConfirmed={setCurrent} />
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
            <ChooseFactor onEnrolled={setCodes} gated={gated} current={current} />
          ) : (
            <TotpEnrolment onEnrolled={setCodes} gated={gated} current={current} />
          )}
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

type Step = { onEnrolled: (codes: string[]) => void; gated: boolean; current: string | null }

/**
 * Replacing a live factor: prove you still hold the current one first.
 *
 * Not a formality. Everything else this screen can reach is behind the second factor;
 * changing the factor itself was the one place a hijacked live session — a stolen cookie, a
 * machine left unlocked — could quietly move the account onto somebody else's phone and keep
 * it. Having verified this session at some point is not the same as holding the device now.
 *
 * The code is not spent here: it is held and handed to the endpoint that starts the
 * enrolment, which is what checks it. Two round trips would mean two codes, and the second
 * one is thirty seconds away.
 */
function ConfirmCurrent({ onConfirmed }: { onConfirmed: (code: string) => void }) {
  const [code, setCode] = useState('')
  return (
    <AuthLayout>
      <Card>
        <CardHeader className="border-b">
          <CardTitle>Confirm it's you</CardTitle>
          <CardDescription>
            Replacing your second factor replaces the only thing standing between your
            password and your account. Enter a current code first.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Form onSubmit={() => onConfirmed(code)}>
            <CodeField
              id="current-code"
              label="Current code"
              autoFocus
              value={code}
              onChange={setCode}
              hint="A recovery code works here too."
            />
            <Button type="submit" className="w-full">
              Continue
            </Button>
            <Button asChild type="button" variant="ghost" className="w-full">
              <Link to="/security">Cancel</Link>
            </Button>
          </Form>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

/** Only shown where the business allows the weaker factor — otherwise there is no choice. */
function ChooseFactor(props: Step) {
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

function TotpEnrolment(props: Step) {
  const [code, setCode] = useState('')
  const [manual, setManual] = useState(false)

  // Started on mount rather than behind a button: by this point there is nothing left to
  // decide, and a "Begin" click between arriving and seeing the QR code is ceremony.
  const enrolment = useQuery({
    queryKey: [...MFA, 'enrolment'],
    queryFn: () => startEnrolment(props.current ?? undefined),
    retry: false,
    staleTime: Infinity,
  })
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

function EmailEnrolment(props: Step) {
  const [code, setCode] = useState('')
  const { user } = useSession()
  const sent = useQuery({
    queryKey: [...MFA, 'email-enrolment'],
    queryFn: async () => {
      await startEmailEnrolment(props.current ?? undefined)
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
 *
 * A gated enrolment (the business forced it) lands on Home rather than Security — #117, spec
 * #113: this is the fresh install's owner finishing the one thing standing between the setup
 * wizard and the "Get your business ready" checklist waiting there, not somebody who came
 * from Security and would expect to land back on it. The server may also have opened an
 * Admin Mode window in the same response (`auth/modes.py::try_first_run_admin_grant`) — the
 * session re-read below is what picks that up.
 */
function EnrolmentDone({ codes, gated }: { codes: string[]; gated: boolean }) {
  const queryClient = useQueryClient()
  const [left, setLeft] = useState(false)

  // A rendered redirect rather than an imperative `navigate()` in the click handler. Both
  // the re-read session and this flag land in the same React pass, so the gate in `App.tsx`
  // is evaluating the answer that cleared it — an imperative navigate can run one render
  // ahead of the cache update, and the gate then sends the browser straight back here with
  // the codes gone.
  if (left) return <Navigate to={gated ? '/' : '/security'} replace />

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
              setLeft(true)
            }}
          >
            I have saved them
          </Button>
        </CardContent>
      </Card>
    </AuthLayout>
  )
}

/**
 * A gated session has nowhere to go back to, so it is offered the only other way off this
 * screen. An ungated one came from Security and can simply return.
 */
function SignOut({ gated }: { gated: boolean }) {
  const signOut = useLogout()
  return gated ? (
    <Button type="button" variant="ghost" className="w-full" onClick={() => signOut.mutate()}>
      Sign out
    </Button>
  ) : (
    <Button asChild type="button" variant="ghost" className="w-full">
      <Link to="/security">Cancel</Link>
    </Button>
  )
}
