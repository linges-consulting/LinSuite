import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Lock, Plus, ShieldCheck, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { Field, Form, FormError } from '@/components/form'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import {
  createRole,
  deleteRole,
  fetchCapabilities,
  fetchRoles,
  updateRole,
  type Capability,
  type Role,
} from '@/lib/api'

const ROLES = ['roles'] as const
const CAPABILITIES = ['capabilities'] as const

/**
 * Roles, and the capabilities toggled on each (PRD §1, §7).
 *
 * The toggles are drawn from the server's registry rather than a list kept here, so a
 * capability added in a later ticket appears with its own description and nothing on this
 * screen has to be edited to know about it. Each toggle shows that description, because
 * "catalog.manage" tells an administrator nothing about what they are handing over.
 */
export function RolesPanel() {
  const roles = useQuery({ queryKey: ROLES, queryFn: fetchRoles })
  const capabilities = useQuery({ queryKey: CAPABILITIES, queryFn: fetchCapabilities })
  const [editing, setEditing] = useState<Role | null>(null)
  const [creating, setCreating] = useState(false)

  if (roles.isPending || capabilities.isPending) {
    return <Skeleton className="h-64 w-full" />
  }
  if (roles.isError || capabilities.isError) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {(roles.error ?? capabilities.error)?.message}
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-4">
        <p className="max-w-3xl text-sm text-muted-foreground">
          A role is a set of things somebody is allowed to do. Every account holds exactly
          one.
        </p>
        <Button onClick={() => setCreating(true)}>
          <Plus aria-hidden />
          New role
        </Button>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        {roles.data?.map((role) => (
          <RoleCard
            key={role.id}
            role={role}
            capabilities={capabilities.data ?? []}
            onEdit={() => setEditing(role)}
          />
        ))}
      </div>

      {creating && (
        <RoleDialog
          capabilities={capabilities.data ?? []}
          onClose={() => setCreating(false)}
        />
      )}
      {editing && (
        <RoleDialog
          role={editing}
          capabilities={capabilities.data ?? []}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  )
}

function RoleCard(props: { role: Role; capabilities: Capability[]; onEdit: () => void }) {
  const { role } = props
  const queryClient = useQueryClient()
  const remove = useMutation({
    mutationFn: () => deleteRole(role.id),
    onSuccess: () => {
      toast.success(`Deleted the ${role.name} role`)
      queryClient.invalidateQueries({ queryKey: ROLES })
    },
    onError: (error) => toast.error(error.message),
  })
  const [confirming, setConfirming] = useState(false)

  // A role somebody holds cannot be deleted — the server refuses it, and saying so here
  // means nobody has to press the button to find out.
  const inUse = role.user_count > 0
  const held = props.capabilities.filter((c) => role.capabilities.includes(c.key))

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {role.name}
          {role.is_system && (
            <Badge variant="secondary">
              <Lock aria-hidden />
              Built-in
            </Badge>
          )}
        </CardTitle>
        <CardDescription>{role.description}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <ul className="flex flex-wrap gap-1.5">
          {held.length === 0 && (
            <li className="text-xs text-muted-foreground">No capabilities yet.</li>
          )}
          {held.map((c) => (
            <li key={c.key}>
              <Badge variant={c.requires_admin_mode ? 'warning' : 'outline'}>
                {c.requires_admin_mode && <ShieldCheck aria-hidden />}
                {c.key}
              </Badge>
            </li>
          ))}
        </ul>
        <p className="text-xs text-muted-foreground">
          {role.user_count} {role.user_count === 1 ? 'person' : 'people'}
        </p>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={props.onEdit}>
            {role.is_system ? 'View' : 'Edit'}
          </Button>
          {!role.is_system && (
            <Button
              variant="ghost"
              size="sm"
              className="ml-auto text-destructive hover:text-destructive"
              disabled={inUse || remove.isPending}
              // The reason travels with the disabled control, so it is readable by anyone
              // wondering why the button does nothing — including a screen reader.
              title={inUse ? `${role.user_count} still hold this role` : undefined}
              aria-label={`Delete ${role.name}`}
              onClick={() => setConfirming(true)}
            >
              <Trash2 aria-hidden />
              Delete
            </Button>
          )}
        </div>
        {!role.is_system && inUse && (
          <p className="text-xs text-muted-foreground">
            Move {role.user_count === 1 ? 'the person' : 'those people'} to another role
            before deleting this one.
          </p>
        )}
      </CardContent>

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Delete the {role.name} role?</DialogTitle>
            <DialogDescription>
              This cannot be undone. Nobody holds this role, so no account changes.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={remove.isPending}
              onClick={() => remove.mutate(undefined, { onSettled: () => setConfirming(false) })}
            >
              Delete role
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}

/**
 * Create or edit. A built-in role opens read-only: the server refuses to change one, and an
 * editable form that always fails on save would be a worse way to learn that.
 */
function RoleDialog(props: { role?: Role; capabilities: Capability[]; onClose: () => void }) {
  const queryClient = useQueryClient()
  const readOnly = props.role?.is_system ?? false
  const [name, setName] = useState(props.role?.name ?? '')
  const [description, setDescription] = useState(props.role?.description ?? '')
  const [held, setHeld] = useState<string[]>(props.role?.capabilities ?? [])

  const save = useMutation({
    mutationFn: () => {
      const draft = { name, description, capabilities: held }
      return props.role ? updateRole(props.role.id, draft) : createRole(draft)
    },
    onSuccess: (role) => {
      toast.success(props.role ? `Saved the ${role.name} role` : `Created the ${role.name} role`)
      queryClient.invalidateQueries({ queryKey: ROLES })
      props.onClose()
    },
  })

  const toggle = (key: string) =>
    setHeld((current) =>
      current.includes(key) ? current.filter((k) => k !== key) : [...current, key],
    )

  const groups = [...new Set(props.capabilities.map((c) => c.group))]

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            {readOnly ? props.role?.name : props.role ? `Edit ${props.role.name}` : 'New role'}
          </DialogTitle>
          <DialogDescription>
            {readOnly
              ? 'This is a built-in role. What it can do is fixed — create a role of your own to choose its capabilities.'
              : 'Choose what somebody holding this role is allowed to do.'}
          </DialogDescription>
        </DialogHeader>

        <Form onSubmit={() => !readOnly && save.mutate()}>
          <Field label="Name" htmlFor="role-name">
            <Input
              id="role-name"
              value={name}
              required
              disabled={readOnly}
              maxLength={64}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          <Field label="Description" htmlFor="role-description">
            <Textarea
              id="role-description"
              value={description}
              disabled={readOnly}
              maxLength={200}
              rows={2}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>

          <fieldset disabled={readOnly} className="flex flex-col gap-4">
            <legend className="sr-only">Capabilities</legend>
            {groups.map((group) => (
              <div key={group} className="flex flex-col gap-2">
                <h3 className="text-xs font-medium text-muted-foreground">{group}</h3>
                {props.capabilities
                  .filter((c) => c.group === group)
                  .map((c) => (
                    <div key={c.key} className="flex items-start gap-2.5">
                      <Checkbox
                        id={`cap-${c.key}`}
                        checked={held.includes(c.key)}
                        disabled={readOnly}
                        onCheckedChange={() => toggle(c.key)}
                        className="mt-0.5"
                      />
                      <div className="flex flex-col gap-0.5">
                        <Label htmlFor={`cap-${c.key}`} className="font-medium">
                          {c.key}
                          {c.requires_admin_mode && (
                            <Badge variant="warning" className="ml-1.5">
                              <ShieldCheck aria-hidden />
                              Admin Mode
                            </Badge>
                          )}
                        </Label>
                        {/* The description is the point of the registry reaching the client:
                            a key alone does not tell anybody what they are granting. */}
                        <p className="text-xs text-muted-foreground">{c.description}</p>
                      </div>
                    </div>
                  ))}
              </div>
            ))}
          </fieldset>

          {save.error && <FormError>{save.error.message}</FormError>}

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={props.onClose}>
              {readOnly ? 'Close' : 'Cancel'}
            </Button>
            {!readOnly && (
              <Button type="submit" disabled={save.isPending || !name.trim()}>
                {save.isPending ? 'Saving…' : props.role ? 'Save role' : 'Create role'}
              </Button>
            )}
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
