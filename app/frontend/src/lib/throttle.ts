import { useEffect, useState } from 'react'
import { ApiError } from '@/lib/api'

/**
 * What a 429 means on screen, counted down live.
 *
 * The server refuses an attempt that is too soon rather than holding the request open, so the
 * wait is the browser's to display — and displaying it honestly is the whole point. "Sign in
 * failed" for an account that is locked for the next quarter of an hour sends someone to retype
 * a password that was never the problem.
 *
 * Two shapes, told apart by the server rather than by guessing from the size of the wait:
 * a delay is seconds and is counted down, a lock is minutes or hours and is quoted as the
 * clock time it reopens — nobody watches a fifteen-minute countdown.
 */
export type Throttle = {
  /** True while the server would refuse another attempt. The submit button follows this. */
  blocked: boolean
  message?: string
}

export function useThrottle(error: unknown): Throttle {
  const retryAt = error instanceof ApiError ? error.retryAt : null
  const locked = error instanceof ApiError && error.locked
  // The remaining time is derived at render from the clock, so it is never a stale copy of
  // one; this state exists only to make the second tick over.
  const [, redraw] = useState(0)

  useEffect(() => {
    if (retryAt === null) return
    const id = setInterval(() => {
      redraw((n) => n + 1)
      // Nothing left to count. Re-enabling the form is the last thing this has to do.
      if (Date.now() >= retryAt) clearInterval(id)
    }, 1000)
    return () => clearInterval(id)
  }, [retryAt])

  if (retryAt === null) return { blocked: false }
  // Reading the clock during render is the point: the remaining time is *meant* to differ on
  // every render, and holding a copy of it in state is what would make it wrong — a snapshot
  // taken before this error arrived would count down from the wrong instant.
  // oxlint-disable-next-line react/purity
  const seconds = Math.ceil((retryAt - Date.now()) / 1000)
  if (seconds <= 0) return { blocked: false }
  return {
    blocked: true,
    message: locked
      ? `This account is temporarily locked until ${clockTime(retryAt)}.`
      : `Too many attempts — try again in ${seconds} s.`,
  }
}

export function clockTime(at: number | string): string {
  return new Date(at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}
