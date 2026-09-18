import { useQueryClient } from '@tanstack/react-query'
import { Briefcase, Check, ShieldCheck } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Field, Form, FormError } from '@/components/form'
import { CodeField } from '@/components/mfa'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
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
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { ApiError, type Mode } from '@/lib/api'
import { SESSION, useSession, useSwitchMode } from '@/lib/auth'
import { clockTime, useThrottle } from '@/lib/throttle'

/**
 * The context switcher (PRD §1).
 *
 * It states which mode is active rather than offering a setting to flip: the badge is the
 * current mode, and choosing the other one is a deliberate act with its own consequences —
 * a password, or the end of an elevated window. Only a user holding both capabilities sees
 * it at all; for everyone else there is no second mode to be in, and a disabled control
 * would only advertise a door they cannot open.
 *
 * Admin Mode carries its remaining time beside the badge. A window that expires silently
 * turns the next click into an unexplained failure, and this is a tool people are using
 * while a client is in the room.
 */
export function ModeSwitcher() {
  const { user } = useSession()
  const switchMode = useSwitchMode()
  const [askingPassword, setAskingPassword] = useState(false)
  // Whether the server has said this window also needs a code. It is asked for only when
  // the twelve-hourly interval has elapsed, so it is discovered from a 403 rather than
  // predicted — the frontend cannot know when the interval ran out.
  const [needsCode, setNeedsCode] = useState(false)

  if (!user?.can_switch_modes) return null

  const admin = user.mode === 'admin'
  const label = admin ? 'Admin Mode' : 'Staff Mode'

  const enterAdmin = () => {
    // A live grant makes this free. If the window lapsed a moment ago and this tab has not
    // heard yet, the server says 403 and the dialog opens after all — the same place the
    // user would have landed had we known.
    if (!user.admin_grant_expires_at) return openDialog()
    switchMode.mutate(
      { mode: 'admin' },
      {
        onError: (error) =>
          error instanceof ApiError &&
          error.status === 403 &&
          openDialog(error.code === 'mfa_required'),
      },
    )
  }

  const openDialog = (withCode = false) => {
    switchMode.reset()
    setNeedsCode(withCode)
    setAskingPassword(true)
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            className="gap-2"
            aria-label={`Switch mode — currently ${label}`}
          >
            <Badge variant={admin ? 'warning' : 'secondary'}>
              {admin ? <ShieldCheck aria-hidden /> : <Briefcase aria-hidden />}
              {label}
            </Badge>
            {admin && user.admin_grant_expires_at && (
              <Countdown until={user.admin_grant_expires_at} />
            )}
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-64">
          <DropdownMenuLabel className="font-normal text-muted-foreground">
            Working as
          </DropdownMenuLabel>
          <ModeItem mode="staff" active={!admin} onSelect={() => switchMode.mutate({ mode: 'staff' })}>
            <Briefcase aria-hidden />
            Staff Mode
          </ModeItem>
          <ModeItem mode="admin" active={admin} onSelect={enterAdmin}>
            <ShieldCheck aria-hidden />
            Admin Mode
          </ModeItem>
          {admin && user.admin_hard_limit_at && (
            <>
              <DropdownMenuSeparator />
              <p className="px-2 py-1.5 text-xs text-muted-foreground">
                Admin Mode ends for good at {clockTime(user.admin_hard_limit_at)}, however busy
                you are.
              </p>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>

      <ReauthDialog
        open={askingPassword}
        onOpenChange={setAskingPassword}
        pending={switchMode.isPending}
        error={switchMode.error?.message}
        throttleError={switchMode.error}
        needsCode={needsCode}
        onSubmit={(password, totp) =>
          switchMode.mutate(
            { mode: 'admin', password, totp },
            {
              onSuccess: () => setAskingPassword(false),
              // The first attempt with a password can still come back asking for a code:
              // a live window excuses the password, never the stale verification. Growing
              // the dialog is the right answer — closing it and reopening would throw away
              // what was already typed.
              onError: (error) =>
                error instanceof ApiError && error.code === 'mfa_required' && setNeedsCode(true),
            },
          )
        }
      />
    </>
  )
}

function ModeItem(props: {
  mode: Mode
  active: boolean
  onSelect: () => void
  children: React.ReactNode
}) {
  return (
    <DropdownMenuItem
      onSelect={props.onSelect}
      // Selecting the mode you are already in would re-record a switch that did not happen.
      disabled={props.active}
      aria-current={props.active ? 'true' : undefined}
    >
      {props.children}
      {props.active && <Check aria-label="Active" className="ml-auto" />}
    </DropdownMenuItem>
  )
}

/**
 * Counts down locally and re-reads the session the moment it hits zero, so the switcher
 * drops back to Staff Mode on its own rather than waiting for the next poll.
 */
function Countdown({ until }: { until: string }) {
  const queryClient = useQueryClient()
  const [seconds, setSeconds] = useState(() => secondsUntil(until))

  useEffect(() => {
    const tick = () => {
      const left = secondsUntil(until)
      setSeconds(left)
      if (left <= 0) {
        // Stop at zero rather than re-asking every second. A browser clock running ahead of
        // the server's would otherwise sit at 0:00 and poll forever; one ask is enough,
        // and the answer brings a new `until` that restarts this.
        clearInterval(id)
        queryClient.invalidateQueries({ queryKey: SESSION })
      }
    }
    const id = setInterval(tick, 1000)
    tick()
    return () => clearInterval(id)
  }, [until, queryClient])

  return (
    <span data-numeric className="text-xs text-muted-foreground">
      {formatRemaining(seconds)} left
    </span>
  )
}

function secondsUntil(iso: string): number {
  return Math.max(0, Math.round((new Date(iso).getTime() - Date.now()) / 1000))
}

function formatRemaining(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

function ReauthDialog(props: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onSubmit: (password: string, totp?: string) => void
  pending: boolean
  error?: string
  throttleError: unknown
  needsCode: boolean
}) {
  return (
    <Dialog open={props.open} onOpenChange={props.onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Enter Admin Mode</DialogTitle>
          <DialogDescription>
            Administration runs in a short window that ends on its own, including while you are
            still working. Confirm your password to start one.
            {props.needsCode &&
              ' Your second factor has not been checked in a while, so a code is needed too — about once every twelve hours, not on every switch.'}
          </DialogDescription>
        </DialogHeader>
        {/* Radix unmounts everything in here when the dialog closes, so the typed password
            goes with it — no effect to reset the field, and nothing left behind on cancel. */}
        <ReauthForm {...props} />
      </DialogContent>
    </Dialog>
  )
}

function ReauthForm(props: {
  onOpenChange: (open: boolean) => void
  onSubmit: (password: string, totp?: string) => void
  pending: boolean
  error?: string
  throttleError: unknown
  needsCode: boolean
}) {
  const [password, setPassword] = useState('')
  const [totp, setTotp] = useState('')
  // The same per-account counter a login feeds: mistyping either of these here is mistyping
  // it anywhere, and a wrong code costs exactly what a wrong password does.
  const throttle = useThrottle(props.throttleError)

  return (
    <Form onSubmit={() => props.onSubmit(password, props.needsCode ? totp : undefined)}>
      <Field
        label="Password"
        htmlFor="reauth-password"
        error={throttle.is429 ? undefined : props.error}
      >
        <Input
          id="reauth-password"
          type="password"
          autoFocus
          required
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          aria-invalid={props.error && !throttle.is429 ? true : undefined}
        />
      </Field>
      {props.needsCode && (
        <CodeField
          id="reauth-totp"
          label="Code from your authenticator"
          value={totp}
          onChange={setTotp}
          hint="A recovery code works here too."
        />
      )}
      {throttle.message && <FormError>{throttle.message}</FormError>}
      <DialogFooter>
        <Button type="button" variant="ghost" onClick={() => props.onOpenChange(false)}>
          Cancel
        </Button>
        <Button type="submit" disabled={props.pending || throttle.blocked}>
          {props.pending ? 'Confirming…' : 'Enter Admin Mode'}
        </Button>
      </DialogFooter>
    </Form>
  )
}
