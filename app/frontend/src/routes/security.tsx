import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, ShieldCheck, ShieldOff } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router'
import { RecoveryCodes } from '@/components/mfa'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { fetchMfaStatus, regenerateRecoveryCodes } from '@/lib/api'
import { MFA } from '@/lib/query-keys'

/**
 * This account's own security, which today is the second factor.
 *
 * Reached from the account menu rather than the sidebar: it is a page about the person
 * signed in, not a section of the business, and every account has one — including the staff
 * accounts that see no Settings link at all.
 *
 * The recovery codes are a count and a button. There is nothing else to show: only their
 * digests survived the one time they were displayed, so "show me my codes" is a thing the
 * server genuinely cannot do, and regenerating is the honest answer to having lost them.
 */
export function SecurityPage() {
  const status = useQuery({ queryKey: MFA, queryFn: fetchMfaStatus })

  if (status.isPending) return <Skeleton className="h-64 w-full max-w-3xl" />
  if (status.isError) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {status.error.message}
      </p>
    )
  }

  const { enrolled, method, recovery_codes_remaining: remaining } = status.data

  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <Card>
        <CardHeader className="border-b">
          <CardTitle className="flex items-center gap-2">
            Two-factor authentication
            <Badge variant={enrolled ? 'success' : 'warning'}>
              {enrolled ? <ShieldCheck aria-hidden /> : <ShieldOff aria-hidden />}
              {enrolled ? 'On' : 'Off'}
            </Badge>
          </CardTitle>
          <CardDescription>
            {enrolled
              ? method === 'email'
                ? 'You get a code by email each time you sign in.'
                : 'You enter a code from your authenticator app each time you sign in.'
              : status.data.required_for_admin
                ? 'This business requires one on accounts that can administer it.'
                : 'A code from your phone, on top of your password.'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button asChild variant={enrolled ? 'outline' : 'default'}>
            <Link to="/mfa/enrol">{enrolled ? 'Set it up again' : 'Set up two-factor'}</Link>
          </Button>
          {enrolled && (
            <p className="mt-2 text-xs text-muted-foreground">
              Setting it up again replaces what you have now, including your recovery codes.
            </p>
          )}
        </CardContent>
      </Card>

      {enrolled && <RecoveryCodesCard remaining={remaining} />}
    </div>
  )
}

function RecoveryCodesCard({ remaining }: { remaining: number }) {
  const queryClient = useQueryClient()
  const [fresh, setFresh] = useState<string[] | null>(null)
  const regenerate = useMutation({
    mutationFn: regenerateRecoveryCodes,
    onSuccess: (codes) => {
      setFresh(codes)
      queryClient.invalidateQueries({ queryKey: MFA })
    },
  })

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle className="flex items-center gap-2">
          <KeyRound aria-hidden className="size-4" />
          Recovery codes
        </CardTitle>
        <CardDescription>
          {/* Never colour alone (DESIGN.md), and never a bare number: "2" means nothing
              without the sentence that says what running out costs. */}
          {remaining === 0
            ? 'You have none left. Without one, getting back in after losing your device needs an administrator.'
            : `${remaining} of your codes are still unused. Each one works once.`}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {fresh ? (
          <RecoveryCodes codes={fresh} />
        ) : (
          <>
            <Button variant="outline" disabled={regenerate.isPending} onClick={() => regenerate.mutate()}>
              {regenerate.isPending ? 'Issuing…' : 'Generate new codes'}
            </Button>
            <p className="text-xs text-muted-foreground">
              The codes you have now stop working the moment new ones are issued.
            </p>
          </>
        )}
        {regenerate.isError && (
          <p role="alert" className="text-sm text-destructive">
            {regenerate.error.message}
          </p>
        )}
      </CardContent>
    </Card>
  )
}
