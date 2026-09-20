import { HOUR_PX } from '@/lib/calendar/pixels'

/**
 * The hour labels down the left. Each sits *on* its grid line (tech-stack §13), not centred
 * in the row: the label is the line's name. Midnight has no label — it is the top edge.
 */
export function TimeGutter(props: { nowMinutes: number | null; nowLabel: string }) {
  return (
    <div className="relative border-r bg-card" aria-hidden>
      {Array.from({ length: 23 }, (_, i) => i + 1).map((hour) => (
        <span
          key={hour}
          className="absolute right-2 -translate-y-1/2 text-xs tabular-nums text-muted-foreground"
          style={{ top: hour * HOUR_PX }}
        >
          {hour % 12 || 12} {hour < 12 ? 'AM' : 'PM'}
        </span>
      ))}
      {props.nowMinutes !== null && (
        <span
          className="absolute right-1 z-10 -translate-y-1/2 whitespace-nowrap rounded-sm bg-destructive px-1 text-xs font-medium tabular-nums text-destructive-foreground"
          style={{ top: props.nowMinutes }}
        >
          {props.nowLabel}
        </span>
      )}
    </div>
  )
}
