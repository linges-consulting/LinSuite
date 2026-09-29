import { useMemo, type ReactNode } from 'react'
import { CallEventSourceContext, DemoCallEventSource } from '@/lib/call-events'

/** Mounted once, in `AppShell`, so the screen-pop panel and the simulate control share one
 *  source for the life of the session — a call simulated from the phone-lookup page still
 *  reaches the panel wherever else it is rendered. */
export function CallEventSourceProvider({ children }: { children: ReactNode }) {
  const source = useMemo(() => new DemoCallEventSource(), [])
  return (
    <CallEventSourceContext.Provider value={source}>{children}</CallEventSourceContext.Provider>
  )
}
