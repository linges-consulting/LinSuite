import { Button } from '@/components/ui/button'
import { clock } from '@/lib/calendar/format'
import type { AvailabilitySlot } from '@/lib/api'

/**
 * A day's bookable starts, as pressable clock times. Extracted from `routes/schedule.tsx`'s
 * staff booking dialog (Phase 6 Task 6, #10) for `/book`'s public slot picker to share rather
 * than reimplement — the component was already presentational (`slots`, `chosen`, `timezone`,
 * `onPick`; no calendar-only state), so nothing about it was staff-authenticated to begin with.
 */
export function SlotButtons(props: {
  slots: AvailabilitySlot[]
  chosen: AvailabilitySlot | null
  timezone?: string
  /** Named when several lists share a dialog, so "10:00 AM under Ana" is its own control. */
  group?: string
  onPick: (slot: AvailabilitySlot) => void
}) {
  return (
    <div className="flex flex-wrap gap-1.5" role="group" aria-label={props.group}>
      {props.slots.map((s) => (
        <Button
          key={s.starts_at}
          type="button"
          size="sm"
          variant={props.chosen?.starts_at === s.starts_at ? 'default' : 'outline'}
          aria-pressed={props.chosen?.starts_at === s.starts_at}
          className="tabular-nums"
          onClick={() => props.onPick(s)}
        >
          {clock(s.starts_at, props.timezone)}
        </Button>
      ))}
    </div>
  )
}
