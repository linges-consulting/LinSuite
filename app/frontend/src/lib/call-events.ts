import { createContext, useContext } from 'react'
import { simulateCall, type CallEvent } from '@/lib/api'

export type { CallEvent } from '@/lib/api'

/**
 * The one seam a real telephony transport would replace (Phase 14, #16 decision #1). Nothing
 * in this codebase pushes to a browser — CLAUDE.md: no WebSocket layer in v1 — so today the
 * only implementation is `DemoCallEventSource`, driven by a click rather than a server. A
 * future VoIP webhook's own event source would implement the same `subscribe`; the screen-pop
 * panel that consumes it (`components/screen-pop-panel.tsx`) never has to change.
 */
export type CallEventSource = {
  /** Registers `listener` for every future call and returns the function that unregisters it
   *  — the same unsubscribe-by-return shape `useEffect`'s own cleanup already expects. */
  subscribe(listener: (event: CallEvent) => void): () => void
}

/**
 * The demo implementation: nothing arrives on its own. `trigger()` is what the simulate
 * control (rendered only while `demo_mode` is on) calls — it asks the server for a fake call
 * and hands it to every subscriber, the same event a real transport would eventually push.
 */
export class DemoCallEventSource implements CallEventSource {
  private listeners = new Set<(event: CallEvent) => void>()

  subscribe(listener: (event: CallEvent) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  async trigger(): Promise<CallEvent> {
    const event = await simulateCall()
    for (const listener of this.listeners) listener(event)
    return event
  }
}

/** The Provider lives in `components/call-event-source-provider.tsx` (JSX needs its own
 *  file for Fast Refresh to treat it as a component) — mounted once, in `AppShell`. */
export const CallEventSourceContext = createContext<DemoCallEventSource | null>(null)

function useSource(): DemoCallEventSource {
  const source = useContext(CallEventSourceContext)
  if (!source) throw new Error('useCallEventSource must be used within CallEventSourceProvider')
  return source
}

/** For a consumer that only ever listens — the screen-pop panel — typed to the abstract
 *  `CallEventSource` so it cannot reach for `trigger()` by accident. */
export function useCallEventSource(): CallEventSource {
  return useSource()
}

/** For the simulate control alone: the one place allowed to call `trigger()`. */
export function useDemoCallTrigger(): DemoCallEventSource {
  return useSource()
}
