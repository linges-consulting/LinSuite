import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { PhoneLookupResultView } from '@/components/phone-lookup-result'
import { SimulateCallButton } from '@/components/simulate-call-button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { fetchDemoMode, lookupPhone } from '@/lib/api'
import { DEMO_MODE, PHONE_LOOKUP } from '@/lib/query-keys'

const MIN_DIGITS = 4

/**
 * The real feature behind the CTI ticket (Phase 14, #16 decision #2): no telephony at all,
 * just a search — a staff member reads digits off a caller ID and gets the same profile
 * summary, classification, previous-provider history and quick-book shortcut the screen-pop
 * panel shows for an actual call. `customers.view`, Staff Mode.
 *
 * The simulate control (decision #1) lives here rather than on the screen-pop panel itself,
 * and renders only once `GET /api/cti/demo-mode` says it is on — with it off, this page has
 * nothing that would even hint the control exists.
 */
export function PhoneLookupPage() {
  const [typed, setTyped] = useState('')
  const digits = typed.replace(/\D/g, '')
  const ready = digits.length >= MIN_DIGITS

  const result = useQuery({
    queryKey: [...PHONE_LOOKUP, digits],
    queryFn: () => lookupPhone(digits),
    enabled: ready,
  })
  const demoMode = useQuery({ queryKey: DEMO_MODE, queryFn: fetchDemoMode })

  return (
    <div className="flex max-w-xl flex-col gap-6">
      <div className="flex flex-col gap-2">
        <Label htmlFor="phone-lookup">Caller's phone number</Label>
        <Input
          id="phone-lookup"
          placeholder="(416) 555-0199"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          autoFocus
        />
        <p className="text-xs text-muted-foreground">
          Enter at least {MIN_DIGITS} digits — formatting is ignored.
        </p>
      </div>

      {digits.length > 0 && !ready && (
        <p className="text-sm text-muted-foreground">Keep typing…</p>
      )}
      {ready && result.isPending && <Skeleton className="h-20 w-full" />}
      {ready && result.isError && (
        <p role="alert" className="text-sm text-destructive">
          {(result.error as Error).message}
        </p>
      )}
      {ready && result.data && <PhoneLookupResultView result={result.data} />}

      {demoMode.data?.enabled && (
        <div className="flex flex-col gap-2 border-t pt-6">
          <p className="text-sm text-muted-foreground">
            Demo mode is on — simulate an incoming call to see the screen-pop panel.
          </p>
          <div>
            <SimulateCallButton />
          </div>
        </div>
      )}
    </div>
  )
}
