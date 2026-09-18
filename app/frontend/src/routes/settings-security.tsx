import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { TriangleAlert } from 'lucide-react'
import { toast } from 'sonner'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { fetchSecurityPolicy, updateSecurityPolicy, type SecurityPolicy } from '@/lib/api'
import { SESSION } from '@/lib/auth'
import { SECURITY } from '@/lib/query-keys'

/**
 * The two multi-factor decisions a business makes (PRD §1, tech-stack §14).
 *
 * Saved on the flip rather than behind a Save button: there are two switches, each is one
 * fact, and a form that can be left half-saved is worse here than a round trip per click.
 *
 * The email-code warning is on screen beside the switch, not in a tooltip and not in the
 * docs. tech-stack §14 asks for the tradeoff to be stated plainly rather than buried, and an
 * administrator who has to hover to find out that a setting weakens the instance has been
 * told nothing.
 */
export function SecurityPanel() {
  const queryClient = useQueryClient()
  const policy = useQuery({ queryKey: SECURITY, queryFn: fetchSecurityPolicy })

  const save = useMutation({
    mutationFn: updateSecurityPolicy,
    onSuccess: (saved) => {
      queryClient.setQueryData(SECURITY, saved)
      // Turning the requirement on can put this very session behind the enrolment gate, so
      // the session is re-read rather than left believing what it knew a moment ago.
      queryClient.invalidateQueries({ queryKey: SESSION })
      toast.success('Security policy saved')
    },
    onError: (error) => {
      toast.error(error.message)
      queryClient.invalidateQueries({ queryKey: SECURITY })
    },
  })

  if (policy.isPending) return <Skeleton className="h-40 w-full" />
  if (policy.isError) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {policy.error.message}
      </p>
    )
  }

  const set = (change: Partial<SecurityPolicy>) => save.mutate({ ...policy.data, ...change })

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <Toggle
        id="mfa-required"
        label="Require two-factor authentication for administrators"
        checked={policy.data.mfa_required_for_admin}
        disabled={save.isPending}
        onChange={(mfa_required_for_admin) => set({ mfa_required_for_admin })}
      >
        Anyone whose role can administer this business is asked to set up a second factor
        before they can do anything else. Turn it off if you work alone and one lost phone
        would lock you out of your own business.
      </Toggle>

      <Toggle
        id="mfa-email-otp"
        label="Allow emailed codes as a second factor"
        checked={policy.data.mfa_email_otp_allowed}
        disabled={save.isPending}
        onChange={(mfa_email_otp_allowed) => set({ mfa_email_otp_allowed })}
      >
        <span className="flex items-start gap-1.5 text-warning">
          <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
          <span>
            Lower assurance than an authenticator app. If somebody has the password, they
            often have the inbox the code would be sent to — and an emailed code is not
            recognised as out-of-band for that reason.
          </span>
        </span>
        <span className="mt-2 block">
          Staff who will not install an authenticator app can use this instead. Emailed codes
          are always available as the last resort when somebody's recovery codes are gone,
          whatever this is set to.
        </span>
      </Toggle>
    </div>
  )
}

function Toggle(props: {
  id: string
  label: string
  checked: boolean
  disabled: boolean
  onChange: (checked: boolean) => void
  children: React.ReactNode
}) {
  return (
    <div className="flex gap-3">
      <Checkbox
        id={props.id}
        className="mt-0.5"
        checked={props.checked}
        disabled={props.disabled}
        onCheckedChange={(checked) => props.onChange(checked === true)}
      />
      <div className="flex flex-col gap-1">
        <Label htmlFor={props.id}>{props.label}</Label>
        <p className="text-xs text-muted-foreground">{props.children}</p>
      </div>
    </div>
  )
}
