import { useEffect, useMemo, useState } from 'react'
import type { RosterEntry } from '@/lib/api'

/**
 * Which practitioners' columns the calendar shows: a per-user, per-device preference
 * (localStorage, keyed by user id), falling back to a rule when nothing usable is stored.
 *
 * **The default.** A practitioner sees their own column only — the calendar opens on what
 * they are there to do. Anybody else (front desk, admin-only) sees every active
 * practitioner, the way the calendar always has.
 *
 * **What is stored** is a concrete list of staff ids, not a mode flag, so "deactivated since
 * it was chosen" is just "no longer in the active practitioner list" — filtered out on
 * every read, silently, with no migration to write.
 */

const key = (userId: string) => `linsuite.schedule.practitioners.${userId}`

function readStored(userId: string): string[] | null {
  try {
    const raw = localStorage.getItem(key(userId))
    if (!raw) return null
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) && parsed.every((id) => typeof id === 'string') ? parsed : null
  } catch {
    // Private browsing, a blocked store, or a quota error: no memory, not a crash.
    return null
  }
}

function writeStored(userId: string, ids: string[]): void {
  try {
    localStorage.setItem(key(userId), JSON.stringify(ids))
  } catch {
    // The pick still works for this render; it just will not survive a reload.
  }
}

export function usePractitionerSelection(roster: RosterEntry[], userId: string | undefined) {
  const practitioners = useMemo(() => roster.filter((m) => m.is_practitioner), [roster])
  const own = useMemo(() => roster.find((m) => m.user_id === userId) ?? null, [roster, userId])
  // A practitioner's own column; everyone else, every active practitioner.
  const defaultIds = useMemo(
    () => (own?.is_practitioner ? [own.id] : practitioners.map((m) => m.id)),
    [own, practitioners],
  )

  const [stored, setStored] = useState<string[] | null>(() => (userId ? readStored(userId) : null))
  // A different account signing in reads its own memory, not the last one's.
  useEffect(() => setStored(userId ? readStored(userId) : null), [userId])

  const validIds = useMemo(() => new Set(practitioners.map((m) => m.id)), [practitioners])
  const kept = useMemo(() => (stored ?? []).filter((id) => validIds.has(id)), [stored, validIds])
  const selected = useMemo(
    () => new Set(kept.length > 0 ? kept : defaultIds),
    [kept, defaultIds],
  )

  const setSelected = (ids: string[]) => {
    setStored(ids)
    if (userId) writeStored(userId, ids)
  }

  return { practitioners, ownId: own?.id ?? null, selected, setSelected }
}
