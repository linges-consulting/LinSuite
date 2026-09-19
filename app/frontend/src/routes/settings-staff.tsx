import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BadgeCheck,
  LockOpen,
  MailPlus,
  MoreHorizontal,
  Pencil,
  Plus,
  ShieldCheck,
  ShieldOff,
  UserCheck,
  UserMinus,
} from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { Field, Form, FormError } from '@/components/form'
import { Badge } from '@/components/ui/badge'
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import {
  assignRole,
  createStaff,
  deactivateStaff,
  fetchRoles,
  fetchStaff,
  fetchStaffPalette,
  reactivateStaff,
  resendInvite,
  resetUserMfa,
  unlockAccount,
  updateStaff,
  type StaffColour,
  type StaffDraft,
  type StaffRow,
} from '@/lib/api'
import { ROLES, STAFF, STAFF_PALETTE } from '@/lib/query-keys'
import { clockTime } from '@/lib/throttle'

/**
 * Settings → Staff: who works here, what they are licensed to do, and what they earn on it
 * (PRD §1, §7).
 *
 * One table rather than an accounts screen and a staff screen. They were never two things —
 * an account exists because somebody works here — and splitting them would mean an
 * administrator adding a colleague in two places and forgetting the second.
 *
 * Adding somebody sends an invitation and never a password. There is no field for one on
 * this screen, deliberately: a password an administrator types is a password they know.
 */
export function StaffPanel() {
  const [includeInactive, setIncludeInactive] = useState(false)
  const staff = useQuery({
    queryKey: [...STAFF, includeInactive],
    queryFn: () => fetchStaff(includeInactive),
    // Ticking the filter is a different query, and without this the table would be replaced
    // by a skeleton for the length of one round trip — a flash, on a checkbox.
    placeholderData: (previous) => previous,
  })
  const roles = useQuery({ queryKey: ROLES, queryFn: fetchRoles })
  const palette = useQuery({ queryKey: STAFF_PALETTE, queryFn: fetchStaffPalette })
  const [editing, setEditing] = useState<StaffRow | null>(null)
  const [creating, setCreating] = useState(false)

  if (staff.isPending || roles.isPending || palette.isPending) {
    return <Skeleton className="h-64 w-full" />
  }
  if (staff.isError || roles.isError || palette.isError) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {(staff.error ?? roles.error ?? palette.error)?.message}
      </p>
    )
  }

  const roleOptions = (roles.data ?? []).map((r) => ({ id: r.id, name: r.name }))

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <p className="max-w-3xl text-sm text-muted-foreground">
          Everyone who works here. Adding somebody creates their account and emails them an
          invitation to choose a password — you never set one for them.
        </p>
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <Checkbox
              id="show-inactive"
              checked={includeInactive}
              onCheckedChange={(on) => setIncludeInactive(on === true)}
            />
            <Label htmlFor="show-inactive" className="font-normal">
              Show inactive
            </Label>
          </div>
          <Button onClick={() => setCreating(true)}>
            <Plus aria-hidden />
            Add staff member
          </Button>
        </div>
      </div>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Name</TableHead>
            <TableHead>Email</TableHead>
            <TableHead>Role</TableHead>
            <TableHead className="text-right">Commission</TableHead>
            {/* Two numbers under one heading, so the column stays narrow enough for the
                actions to stay on screen. The unit is spelled out on each cell. */}
            <TableHead>Status</TableHead>
            <TableHead>Two-factor</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {staff.data?.map((member) => (
            <StaffLine
              key={member.id}
              member={member}
              roles={roleOptions}
              palette={palette.data ?? []}
              onEdit={() => setEditing(member)}
            />
          ))}
        </TableBody>
      </Table>

      {creating && (
        <StaffDialog
          roles={roleOptions}
          palette={palette.data ?? []}
          onClose={() => setCreating(false)}
        />
      )}
      {editing && (
        <StaffDialog
          member={editing}
          roles={roleOptions}
          palette={palette.data ?? []}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  )
}

type RoleOption = { id: string; name: string }

/** The swatch the calendar will paint this person's appointments with. */
function Swatch({ colour, label }: { colour?: StaffColour; label: string }) {
  return (
    <span
      aria-hidden
      title={colour?.name ?? label}
      className="size-3 shrink-0 rounded-full ring-1 ring-foreground/10"
      style={{ backgroundColor: colour?.hex ?? 'transparent' }}
    />
  )
}

function StaffLine(props: {
  member: StaffRow
  roles: RoleOption[]
  palette: StaffColour[]
  onEdit: () => void
}) {
  const { member } = props
  const queryClient = useQueryClient()
  // The key prefix, so both cached rosters — with and without the inactive rows — are
  // re-read. Invalidating only the one on screen leaves the other stale behind the filter.
  const refresh = () => queryClient.invalidateQueries({ queryKey: STAFF })
  const colour = props.palette.find((c) => c.key === member.colour)

  const toastOk = (message: string, description?: string) => () => {
    toast.success(message, description ? { description } : undefined)
    refresh()
  }
  const toastErr = (error: Error) => {
    toast.error(error.message)
    refresh()
  }

  const assign = useMutation({
    mutationFn: (roleId: string) => assignRole(member.user_id, roleId),
    onSuccess: (updated) => {
      toast.success(`${member.display_name} is now ${updated.role}`)
      refresh()
    },
    onError: toastErr,
  })
  const unlock = useMutation({
    mutationFn: () => unlockAccount(member.user_id),
    onSuccess: toastOk(`Unlocked ${member.email}`),
    onError: toastErr,
  })
  const resetMfa = useMutation({
    mutationFn: () => resetUserMfa(member.user_id),
    onSuccess: toastOk(
      `Reset the second factor on ${member.email}`,
      'They are signed out everywhere and will be asked to set one up again.',
    ),
    onError: toastErr,
  })
  const invite = useMutation({
    mutationFn: () => resendInvite(member.id),
    onSuccess: toastOk(
      `Sent ${member.email} a new invitation`,
      'Any earlier link has stopped working.',
    ),
    onError: toastErr,
  })
  const setActive = useMutation({
    mutationFn: (active: boolean) =>
      active ? reactivateStaff(member.id) : deactivateStaff(member.id),
    onSuccess: (updated) =>
      toastOk(
        updated.active
          ? `${updated.display_name} can sign in again`
          : `Deactivated ${updated.display_name}`,
        updated.active ? undefined : 'They are signed out everywhere. Their history is kept.',
      )(),
    onError: toastErr,
  })

  return (
    // Inactive rows are greyed rather than hidden when the filter is on — they are history,
    // and history that looks identical to the roster is the wrong kind of quiet.
    <TableRow className={member.active ? undefined : 'opacity-55'}>
      <TableCell>
        <div className="flex items-center gap-2">
          <Swatch colour={colour} label={member.colour} />
          <span className="font-medium">{member.display_name}</span>
          {member.is_practitioner && (
            <Badge variant="info" title="Printed on treatment receipts">
              <BadgeCheck aria-hidden />
              {[member.designation, member.licence_number].filter(Boolean).join(' ')}
            </Badge>
          )}
        </div>
      </TableCell>
      <TableCell>
        <div className="flex items-center gap-2">
          <span className="text-muted-foreground">{member.email}</span>
          {member.invite_pending && <Badge variant="warning">Invited</Badge>}
        </div>
      </TableCell>
      <TableCell>
        <Select
          value={member.role_id}
          disabled={assign.isPending || !member.active}
          onValueChange={(roleId) => roleId !== member.role_id && assign.mutate(roleId)}
        >
          <SelectTrigger className="w-44" aria-label={`Role for ${member.email}`}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {props.roles.map((role) => (
              <SelectItem key={role.id} value={role.id}>
                {role.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </TableCell>
      <TableCell
        className="text-right whitespace-nowrap tabular-nums"
        data-numeric
        title="Services · retail"
      >
        {`${percent(member.commission_rate_services_bp)}% · ` +
          `${percent(member.commission_rate_retail_bp)}%`}
      </TableCell>
      <TableCell>
        {/* Status is never colour alone (DESIGN.md) — each badge carries the word. */}
        {!member.active ? (
          <Badge variant="secondary">Inactive</Badge>
        ) : member.locked_until ? (
          <Badge variant="destructive">Locked until {clockTime(member.locked_until)}</Badge>
        ) : (
          <span className="text-muted-foreground">Active</span>
        )}
      </TableCell>
      <TableCell>
        <Badge variant={member.mfa_enrolled ? 'success' : 'secondary'}>
          {member.mfa_enrolled ? <ShieldCheck aria-hidden /> : <ShieldOff aria-hidden />}
          {member.mfa_enrolled ? 'On' : 'Off'}
        </Badge>
      </TableCell>
      <TableCell className="text-right">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="sm" aria-label={`Actions for ${member.email}`}>
              <MoreHorizontal aria-hidden />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-40">
            <DropdownMenuItem onSelect={props.onEdit}>
              <Pencil aria-hidden />
              Edit
            </DropdownMenuItem>
            <DropdownMenuItem
              disabled={!member.invite_pending || invite.isPending}
              onSelect={() => invite.mutate()}
            >
              <MailPlus aria-hidden />
              Resend invite
            </DropdownMenuItem>
            <DropdownMenuItem
              disabled={!member.locked_until || unlock.isPending}
              onSelect={() => unlock.mutate()}
            >
              <LockOpen aria-hidden />
              Unlock
            </DropdownMenuItem>
            {/* Disabled where there is nothing to reset: offered identically either way, it
                is a button whose only effect on an unenrolled account is signing them out of
                every device for no reason. */}
            <DropdownMenuItem
              disabled={!member.mfa_enrolled || resetMfa.isPending}
              onSelect={() => {
                if (
                  confirm(
                    `Reset the second factor on ${member.email}?\n\n` +
                      'Their authenticator and recovery codes stop working, and they are ' +
                      'signed out everywhere. Only do this if they have lost access.',
                  )
                )
                  resetMfa.mutate()
              }}
            >
              <ShieldOff aria-hidden />
              Reset MFA
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            {/* Destructive, apart from the rest, and it confirms first (DESIGN.md). */}
            {member.active ? (
              <DropdownMenuItem
                variant="destructive"
                disabled={setActive.isPending}
                onSelect={() => {
                  if (
                    confirm(
                      `Deactivate ${member.display_name}?\n\n` +
                        'They are signed out everywhere and cannot sign in again until you ' +
                        'restore them. Their appointments, notes and receipts are kept.',
                    )
                  )
                    setActive.mutate(false)
                }}
              >
                <UserMinus aria-hidden />
                Deactivate
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem
                disabled={setActive.isPending}
                onSelect={() => setActive.mutate(true)}
              >
                <UserCheck aria-hidden />
                Reactivate
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </TableCell>
    </TableRow>
  )
}

// Basis points are what the server stores and what commission is snapshotted in; a person
// thinks in percent. The conversion lives here and nowhere else.
const percent = (basisPoints: number) => Math.round(basisPoints) / 100
const basisPoints = (percentage: string) => Math.round(Number(percentage || 0) * 100)

/**
 * Create or edit. The email and the role are only on the create form: changing an address is
 * a different act from editing a person, and the role has its own select on the row.
 */
function StaffDialog(props: {
  member?: StaffRow
  roles: RoleOption[]
  palette: StaffColour[]
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const existing = props.member
  const [email, setEmail] = useState('')
  const [roleId, setRoleId] = useState(
    existing?.role_id ?? props.roles.find((r) => r.name === 'Staff')?.id ?? '',
  )
  const [firstName, setFirstName] = useState(existing?.first_name ?? '')
  const [lastName, setLastName] = useState(existing?.last_name ?? '')
  const [displayName, setDisplayName] = useState(existing?.display_name ?? '')
  const [isPractitioner, setIsPractitioner] = useState(existing?.is_practitioner ?? false)
  const [designation, setDesignation] = useState(existing?.designation ?? '')
  const [licence, setLicence] = useState(existing?.licence_number ?? '')
  const [services, setServices] = useState(String(percent(existing?.commission_rate_services_bp ?? 0)))
  const [retail, setRetail] = useState(String(percent(existing?.commission_rate_retail_bp ?? 0)))
  const [colour, setColour] = useState(existing?.colour ?? '')
  const [concurrent, setConcurrent] = useState(String(existing?.max_concurrent_appointments ?? 1))

  const suggested = `${firstName} ${lastName}`.trim()
  const draft: StaffDraft = {
    first_name: firstName.trim(),
    last_name: lastName.trim(),
    display_name: displayName.trim() || null,
    is_practitioner: isPractitioner,
    designation: isPractitioner ? designation.trim() || null : null,
    licence_number: isPractitioner ? licence.trim() || null : null,
    commission_rate_services_bp: basisPoints(services),
    commission_rate_retail_bp: basisPoints(retail),
    colour: colour || null,
    max_concurrent_appointments: Math.max(1, Number(concurrent) || 1),
  }

  const save = useMutation({
    mutationFn: () =>
      existing
        ? updateStaff(existing.id, draft)
        : createStaff({ ...draft, email: email.trim(), role_id: roleId }),
    onSuccess: (member) => {
      toast.success(
        existing ? `Saved ${member.display_name}` : `Invited ${member.email}`,
        existing
          ? undefined
          : { description: 'They have an email with a link to choose a password.' },
      )
      queryClient.invalidateQueries({ queryKey: STAFF })
      props.onClose()
    },
  })

  // The same rule the server enforces, said before the round trip rather than instead of it:
  // a practitioner with no licence number is a treatment receipt an insurer will reject.
  const missingCredentials = isPractitioner && !(designation.trim() && licence.trim())
  const incomplete =
    !firstName.trim() || !lastName.trim() || missingCredentials || (!existing && !email.trim())

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{existing ? `Edit ${existing.display_name}` : 'Add staff member'}</DialogTitle>
          <DialogDescription>
            {existing
              ? 'Their credentials, rates and calendar colour.'
              : 'This creates their account and emails them an invitation to choose a password.'}
          </DialogDescription>
        </DialogHeader>

        <Form onSubmit={() => !incomplete && save.mutate()}>
          {!existing && (
            <div className="grid gap-5 sm:grid-cols-2">
              <Field label="Email" htmlFor="staff-email" hint="Where the invitation goes.">
                <Input
                  id="staff-email"
                  type="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </Field>
              <Field label="Role" htmlFor="staff-role">
                <Select value={roleId} onValueChange={setRoleId}>
                  <SelectTrigger id="staff-role">
                    <SelectValue placeholder="Choose a role" />
                  </SelectTrigger>
                  <SelectContent>
                    {props.roles.map((role) => (
                      <SelectItem key={role.id} value={role.id}>
                        {role.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
            </div>
          )}

          <div className="grid gap-5 sm:grid-cols-2">
            <Field label="First name" htmlFor="staff-first">
              <Input
                id="staff-first"
                required
                maxLength={100}
                value={firstName}
                onChange={(e) => setFirstName(e.target.value)}
              />
            </Field>
            <Field label="Last name" htmlFor="staff-last">
              <Input
                id="staff-last"
                required
                maxLength={100}
                value={lastName}
                onChange={(e) => setLastName(e.target.value)}
              />
            </Field>
          </div>

          <Field
            label="Display name"
            htmlFor="staff-display"
            hint="What the calendar and receipts call them. Leave blank to use their name."
          >
            <Input
              id="staff-display"
              maxLength={200}
              placeholder={suggested}
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
            />
          </Field>

          <div className="flex flex-col gap-4 rounded-xl border border-input p-4">
            <div className="flex items-start gap-2.5">
              <Checkbox
                id="staff-practitioner"
                checked={isPractitioner}
                onCheckedChange={(on) => setIsPractitioner(on === true)}
                className="mt-0.5"
              />
              <div className="flex flex-col gap-0.5">
                <Label htmlFor="staff-practitioner" className="font-medium">
                  Practitioner
                </Label>
                <p className="text-xs text-muted-foreground">
                  They deliver treatments. Their designation and licence number are printed on
                  treatment receipts — insurers reject claims without them.
                </p>
              </div>
            </div>
            {isPractitioner && (
              <div className="grid gap-5 sm:grid-cols-2">
                <Field
                  label="Designation"
                  htmlFor="staff-designation"
                  error={
                    isPractitioner && !designation.trim() ? 'A practitioner needs one.' : undefined
                  }
                  hint="RMT, RAc, DC…"
                >
                  <Input
                    id="staff-designation"
                    maxLength={64}
                    value={designation}
                    onChange={(e) => setDesignation(e.target.value)}
                  />
                </Field>
                <Field
                  label="Licence number"
                  htmlFor="staff-licence"
                  error={isPractitioner && !licence.trim() ? 'A practitioner needs one.' : undefined}
                >
                  <Input
                    id="staff-licence"
                    maxLength={64}
                    value={licence}
                    onChange={(e) => setLicence(e.target.value)}
                  />
                </Field>
              </div>
            )}
          </div>

          <div className="grid gap-5 sm:grid-cols-2">
            <Field
              label="Commission on services"
              htmlFor="staff-services"
              hint="Percent of pre-tax, post-discount revenue."
            >
              <Input
                id="staff-services"
                type="number"
                min={0}
                max={100}
                step={0.01}
                value={services}
                onChange={(e) => setServices(e.target.value)}
              />
            </Field>
            <Field
              label="Commission on retail"
              htmlFor="staff-retail"
              hint="Salons usually pay less on product than on services."
            >
              <Input
                id="staff-retail"
                type="number"
                min={0}
                max={100}
                step={0.01}
                value={retail}
                onChange={(e) => setRetail(e.target.value)}
              />
            </Field>
          </div>

          <Field
            label="Concurrent appointments"
            htmlFor="staff-concurrent"
            hint="How many they can run at once. 2 is the stylist with a second chair processing colour."
          >
            <Input
              id="staff-concurrent"
              type="number"
              min={1}
              step={1}
              className="w-24"
              value={concurrent}
              onChange={(e) => setConcurrent(e.target.value)}
            />
          </Field>

          <fieldset className="flex flex-col gap-2">
            <legend className="mb-2 text-sm font-medium">Calendar colour</legend>
            <p className="text-xs text-muted-foreground">
              Every appointment of theirs is painted in it.
              {existing ? '' : ' Left alone, they get the first one nobody is using.'}
            </p>
            <div className="flex flex-wrap gap-2">
              {props.palette.map((c) => (
                <button
                  key={c.key}
                  type="button"
                  aria-label={c.name}
                  aria-pressed={colour === c.key}
                  title={c.name}
                  onClick={() => setColour(c.key)}
                  style={{ backgroundColor: c.hex, color: c.foreground }}
                  className={
                    'flex size-8 items-center justify-center rounded-lg ring-offset-2 ' +
                    'ring-offset-background transition-transform ' +
                    (colour === c.key ? 'ring-2 ring-foreground' : 'ring-1 ring-foreground/10')
                  }
                >
                  {colour === c.key && <ShieldCheck aria-hidden className="size-4" />}
                </button>
              ))}
            </div>
          </fieldset>

          {save.error && <FormError>{save.error.message}</FormError>}

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={props.onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || incomplete}>
              {save.isPending
                ? 'Saving…'
                : existing
                  ? 'Save staff member'
                  : 'Add and send invitation'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
