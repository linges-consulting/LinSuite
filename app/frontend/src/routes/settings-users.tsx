import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { LockOpen, ShieldOff } from 'lucide-react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
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
  fetchAccounts,
  fetchRoles,
  resetUserMfa,
  unlockAccount,
  type AccountRow,
} from '@/lib/api'
import { ACCOUNTS, ROLES } from '@/lib/query-keys'
import { clockTime } from '@/lib/throttle'

/**
 * Who has an account, what role they hold, and the early unlock (PRD §7).
 *
 * The role is a select rather than a screen of its own: changing somebody's role is one
 * decision, and making it a two-step navigation would be ceremony around a dropdown. The
 * refusals that matter — moving the last administrator off the job — come back from the
 * server as a toast, because they depend on the state of every other account and this screen
 * cannot honestly predict them.
 */
export function UsersPanel() {
  const accounts = useQuery({ queryKey: ACCOUNTS, queryFn: fetchAccounts })
  const roles = useQuery({ queryKey: ROLES, queryFn: fetchRoles })

  if (accounts.isPending || roles.isPending) return <Skeleton className="h-48 w-full" />
  if (accounts.isError || roles.isError) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {(accounts.error ?? roles.error)?.message}
      </p>
    )
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Email</TableHead>
          <TableHead>Role</TableHead>
          <TableHead>Status</TableHead>
          <TableHead className="text-right">Two-factor</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {accounts.data?.map((account) => (
          <AccountLine
            key={account.id}
            account={account}
            roles={(roles.data ?? []).map((r) => ({ id: r.id, name: r.name }))}
          />
        ))}
      </TableBody>
    </Table>
  )
}

function AccountLine(props: { account: AccountRow; roles: { id: string; name: string }[] }) {
  const { account } = props
  const queryClient = useQueryClient()
  const refresh = () => queryClient.invalidateQueries({ queryKey: ACCOUNTS })

  const assign = useMutation({
    mutationFn: (roleId: string) => assignRole(account.id, roleId),
    onSuccess: (updated) => {
      toast.success(`${updated.email} is now ${updated.role}`)
      refresh()
    },
    // The server is the only thing that knows whether this was the last administrator, so
    // its sentence is the one shown. Re-reading puts the select back where it was.
    onError: (error) => {
      toast.error(error.message)
      refresh()
    },
  })

  const unlock = useMutation({
    mutationFn: () => unlockAccount(account.id),
    onSuccess: () => {
      toast.success(`Unlocked ${account.email}`)
      refresh()
    },
    onError: (error) => toast.error(error.message),
  })

  const resetMfa = useMutation({
    mutationFn: () => resetUserMfa(account.id),
    onSuccess: () => {
      toast.success(`Reset the second factor on ${account.email}`, {
        description: 'They are signed out everywhere and will be asked to set one up again.',
      })
      refresh()
    },
    onError: (error) => toast.error(error.message),
  })

  return (
    <TableRow>
      <TableCell className="font-medium">{account.email}</TableCell>
      <TableCell>
        <Select
          value={account.role_id}
          disabled={assign.isPending}
          onValueChange={(roleId) => roleId !== account.role_id && assign.mutate(roleId)}
        >
          <SelectTrigger className="w-48" aria-label={`Role for ${account.email}`}>
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
      <TableCell>
        {account.locked_until ? (
          <div className="flex items-center gap-2">
            {/* Status is never colour alone (DESIGN.md) — the badge carries the word too. */}
            <Badge variant="destructive">Locked until {clockTime(account.locked_until)}</Badge>
            <Button
              variant="outline"
              size="sm"
              disabled={unlock.isPending}
              onClick={() => unlock.mutate()}
            >
              <LockOpen aria-hidden />
              Unlock
            </Button>
          </div>
        ) : (
          <span className="text-muted-foreground">Active</span>
        )}
      </TableCell>
      <TableCell className="text-right">
        {/* Destructive and apart from the primary controls, and it confirms first
            (DESIGN.md): this takes a protection off somebody's account and signs them out
            of every device, and there is no undo — the recovery codes are gone too. */}
        <Button
          variant="outline"
          size="sm"
          disabled={resetMfa.isPending}
          onClick={() => {
            if (
              confirm(
                `Reset the second factor on ${account.email}?\n\n` +
                  'Their authenticator and recovery codes stop working, and they are signed ' +
                  'out everywhere. Only do this if they have lost access.',
              )
            )
              resetMfa.mutate()
          }}
        >
          <ShieldOff aria-hidden />
          Reset MFA
        </Button>
      </TableCell>
    </TableRow>
  )
}
