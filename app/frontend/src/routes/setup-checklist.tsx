import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, ChevronLeft, ChevronRight } from 'lucide-react'
import { type ComponentType, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router'
import { AdminModeRequiredNotice } from '@/components/require-admin-mode'
import { Button } from '@/components/ui/button'
import { fetchStaff, fetchStaffPalette, type OnboardingStepKey } from '@/lib/api'
import { useCan } from '@/lib/capability-gate'
import { useSession } from '@/lib/auth'
import { STEP_LABEL, STEP_ORDER } from '@/lib/onboarding-steps'
import { STAFF, STAFF_PALETTE } from '@/lib/query-keys'
import { BrandingPanel } from '@/routes/settings-branding'
import { BusinessPanel } from '@/routes/settings-business'
import { NotificationsPanel } from '@/routes/settings-notifications'
import { ServicesPanel } from '@/routes/settings-services'
import { StaffDialog, StaffPanel } from '@/routes/settings-staff'
import { TaxSettingsPanel } from '@/routes/settings-tax'

function isStepKey(value: string | undefined): value is OnboardingStepKey {
  return !!value && (STEP_ORDER as string[]).includes(value)
}

/** Every step's real Settings panel, reused rather than copied (#117's own instruction). `hours`
 *  opens Staff too: hours are per-staff, edited from a dialog on a staff row
 *  (`staff-availability.tsx`), and there is no business-wide hours screen to point at instead. */
const STEP_PANEL: Record<OnboardingStepKey, ComponentType> = {
  business: BusinessPanel,
  hours: StaffPanel,
  tax: TaxSettingsPanel,
  services: ServicesPanel,
  staff: StaffPanel,
  email: NotificationsPanel,
  branding: BrandingPanel,
}

/**
 * A checklist step, full-page (#117, spec #113): the same Settings panel the tab used to
 * show, framed with Back/Next in the checklist's own fixed order and a link back to it.
 *
 * Gated like the checklist itself — `useCan('admin')` (Admin Mode *and* the `admin`
 * capability) — rather than `RequireAdminMode`'s plain mode check: these pages exist only for
 * the account the checklist is drawn for, the same reasoning `onboarding-checklist.tsx`
 * already gives for asking nothing of the server otherwise.
 */
export function SetupChecklistStepPage() {
  const canAdmin = useCan('admin')
  const { step } = useParams()

  if (!canAdmin) return <AdminModeRequiredNotice />
  if (!isStepKey(step)) return <Navigate to="/" replace />

  const index = STEP_ORDER.indexOf(step)
  const Panel = STEP_PANEL[step]
  const previous = STEP_ORDER[index - 1]
  const next = STEP_ORDER[index + 1]

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between gap-4">
        <Link
          to="/"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-4" aria-hidden />
          Back to checklist
        </Link>
        <p className="text-xs font-medium text-muted-foreground">
          Step {index + 1} of {STEP_ORDER.length}
        </p>
      </div>

      <h1 className="text-base font-semibold tracking-tight">{STEP_LABEL[step]}</h1>

      {step === 'staff' && <AddMeAsPractitioner />}

      <Panel />

      <div className="flex justify-between border-t pt-4">
        <Button variant="outline" asChild>
          <Link to={previous ? `/setup-checklist/${previous}` : '/'}>
            <ChevronLeft aria-hidden />
            Back
          </Link>
        </Button>
        <Button asChild>
          <Link to={next ? `/setup-checklist/${next}` : '/'}>
            {next ? 'Next' : 'Done'}
            <ChevronRight aria-hidden />
          </Link>
        </Button>
      </div>
    </div>
  )
}

/**
 * "Add me as a practitioner" (#117, spec #113 user story 15): the smallest shortcut that
 * makes sense given the schema, not a literal one-click action.
 *
 * The setup wizard already gave the signed-in administrator a staff row (`auth/setup.py`) —
 * every account is a staff member, one-to-one — so there is never a staff member to *create*
 * here. What this does is find that existing row and open it, pre-checked as a practitioner,
 * in the exact same `StaffDialog` the Staff panel itself uses: no second form, no new backend
 * endpoint, just `PATCH /admin/staff/{id}` with `is_practitioner: true` once the designation
 * and licence number are filled in.
 *
 * Those two fields are why this cannot be a true zero-input single click: the database
 * refuses `is_practitioner` without both (`ck_staff_practitioner_credentials`) because
 * insurers reject treatment receipts that lack them, and inventing a placeholder value would
 * be worse than asking. The shortcut is everything up to that: finding the row, checking the
 * box, and reusing the one form that already validates and saves it.
 */
function AddMeAsPractitioner() {
  const { user } = useSession()
  const staff = useQuery({ queryKey: [...STAFF, false], queryFn: () => fetchStaff(false) })
  const palette = useQuery({ queryKey: STAFF_PALETTE, queryFn: fetchStaffPalette })
  const [open, setOpen] = useState(false)

  const mine = staff.data?.find((s) => s.user_id === user?.id)
  if (!mine || mine.is_practitioner) return null

  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-input bg-muted/40 p-4">
        <div className="flex flex-col gap-0.5">
          <p className="text-sm font-medium">Delivering treatments yourself?</p>
          <p className="text-xs text-muted-foreground">
            Add your own designation and licence number — they print on treatment receipts.
          </p>
        </div>
        <Button size="sm" onClick={() => setOpen(true)}>
          Add me as a practitioner
        </Button>
      </div>
      {open && (
        <StaffDialog
          member={{ ...mine, is_practitioner: true }}
          roles={[]}
          palette={palette.data ?? []}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  )
}
