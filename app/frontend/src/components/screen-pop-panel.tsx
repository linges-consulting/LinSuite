import { useQuery } from '@tanstack/react-query'
import { PhoneIncoming, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { PhoneLookupResultView } from '@/components/phone-lookup-result'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { lookupPhone, type CallEvent } from '@/lib/api'
import { useCallEventSource } from '@/lib/call-events'
import { formatPhone } from '@/lib/phone'
import { PHONE_LOOKUP } from '@/lib/query-keys'

/**
 * Mounted once in `AppShell`, so an incoming call reaches the front desk wherever they are in
 * the app. Subscribes to whatever `CallEventSource` the Context provides (Phase 14, #16
 * decision #1) — today always `DemoCallEventSource`, but this component reads it only through
 * `subscribe`, never `trigger`, so swapping in a real transport later changes only the
 * Provider, not this file. Renders nothing until a call actually arrives.
 */
export function ScreenPopPanel() {
  const source = useCallEventSource()
  const [call, setCall] = useState<CallEvent | null>(null)

  useEffect(() => source.subscribe(setCall), [source])

  const result = useQuery({
    queryKey: [...PHONE_LOOKUP, call?.phone ?? ''],
    queryFn: () => lookupPhone(call?.phone ?? ''),
    enabled: call !== null,
  })

  if (!call) return null

  return (
    <div className="fixed right-4 bottom-4 z-50 w-80">
      <Card>
        <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
          <CardTitle className="flex items-center gap-2 text-sm">
            <PhoneIncoming className="size-4" aria-hidden />
            Incoming call
          </CardTitle>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Dismiss"
            onClick={() => setCall(null)}
          >
            <X className="size-4" />
          </Button>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <p className="text-sm tabular-nums text-muted-foreground">{formatPhone(call.phone)}</p>
          {result.isPending && <p className="text-sm text-muted-foreground">Looking up…</p>}
          {result.data && <PhoneLookupResultView result={result.data} />}
        </CardContent>
      </Card>
    </div>
  )
}
