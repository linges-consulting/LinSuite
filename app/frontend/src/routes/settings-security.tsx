import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { TriangleAlert } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import {
  fetchSecurityPolicy,
  updateSecurityPolicy,
  type RetentionProfile,
  type SecurityPolicy,
} from '@/lib/api'
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

  // Only the two switches: the retention profile is saved on its own, explicitly, below —
  // a flip here must never "choose" it on the administrator's behalf.
  const set = (change: Partial<SecurityPolicy>) =>
    save.mutate({
      mfa_required_for_admin: policy.data.mfa_required_for_admin,
      mfa_email_otp_allowed: policy.data.mfa_email_otp_allowed,
      ...change,
    })

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

      <RetentionSection policy={policy.data} />
    </div>
  )
}

const PROFILES: { value: RetentionProfile; label: string; explanation: string }[] = [
  {
    value: 'regulated_health',
    label: 'Regulated health practice',
    explanation:
      'The retention obligation wins. A client record is kept for 10 years after its last ' +
      'clinical entry, or until 10 years after the client turns 18 — whichever is later — ' +
      'and a deletion request never removes a record under that hold.',
  },
  {
    value: 'general_business',
    label: 'General business',
    explanation:
      'No statutory retention obligation, so a deletion request is honoured promptly. For ' +
      'salons, consultants and other businesses that keep no clinical records.',
  },
]

/**
 * Which obligation wins (ADR-0001, tech-stack §16). Saved behind its own button rather than
 * on the click: it re-computes every client's hold. Moving off `regulated_health` releases
 * them all, so that direction asks first and says so. Until somebody has saved a choice the
 * default (`regulated_health`, the recoverable mistake) is a default, and a banner says so.
 */
function RetentionSection({ policy }: { policy: SecurityPolicy }) {
  const queryClient = useQueryClient()
  // Only an unsaved edit is local state; otherwise the radio shows whatever the server last
  // said, so another administrator's save shows up on the next read without clobbering an
  // edit somebody here is in the middle of.
  const [draft, setDraft] = useState<RetentionProfile | null>(null)
  const choice = draft ?? policy.retention_profile
  const setChoice = (profile: RetentionProfile) =>
    setDraft(profile === policy.retention_profile ? null : profile)
  const [confirming, setConfirming] = useState(false)

  const save = useMutation({
    mutationFn: (retention_profile: RetentionProfile) => updateSecurityPolicy({ retention_profile }),
    onSuccess: (saved) => {
      queryClient.setQueryData(SECURITY, saved)
      setDraft(null)
      setConfirming(false)
      toast.success('Retention profile saved')
    },
    onError: (error) => toast.error(error.message),
  })

  const releasesHolds =
    policy.retention_profile === 'regulated_health' && choice === 'general_business'

  return (
    <section aria-labelledby="retention-heading" className="flex flex-col gap-3 border-t pt-6">
      <h3 id="retention-heading" className="text-sm font-medium">
        Record retention
      </h3>
      {!policy.retention_profile_chosen && (
        <p
          role="status"
          className="flex items-start gap-1.5 rounded-md border border-warning/40 bg-warning/10 p-3 text-sm text-warning"
        >
          <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
          <span>
            Choose a retention profile. Until you save one, this business is treated as a
            regulated health practice and client records are held accordingly.
          </span>
        </p>
      )}
      <fieldset className="flex flex-col gap-3">
        <legend className="sr-only">Retention profile</legend>
        {PROFILES.map((p) => (
          <div key={p.value} className="flex gap-3">
            <input
              type="radio"
              id={`retention-${p.value}`}
              name="retention-profile"
              className="mt-1 size-4 accent-primary"
              checked={choice === p.value}
              disabled={save.isPending}
              onChange={() => setChoice(p.value)}
            />
            <div className="flex flex-col gap-1">
              <Label htmlFor={`retention-${p.value}`}>{p.label}</Label>
              <p className="text-xs text-muted-foreground">{p.explanation}</p>
            </div>
          </div>
        ))}
      </fieldset>
      <p className="text-xs text-muted-foreground">
        These periods follow the Ontario health colleges' rule. Confirm them for your practice
        with your regulatory college or counsel.
      </p>
      <div>
        <Button
          size="sm"
          disabled={save.isPending}
          onClick={() => (releasesHolds ? setConfirming(true) : save.mutate(choice))}
        >
          Save retention profile
        </Button>
      </div>

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Switch to general business?</DialogTitle>
            <DialogDescription asChild>
              <div className="flex flex-col gap-3 text-left">
                <p>
                  Every client record held for retention will no longer be held. A deletion
                  request will then remove it, and a removed record cannot be recovered.
                </p>
                <p>
                  Only do this if your business has no statutory obligation to keep clinical
                  records. This is recorded in the audit log.
                </p>
              </div>
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => save.mutate(choice)}
              disabled={save.isPending}
            >
              {save.isPending ? 'Releasing…' : 'Release retention holds'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
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
