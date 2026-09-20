import type { Column } from '@/components/calendar/types'
import type { Schedule } from '@/lib/api'
import { localDate } from '@/lib/calendar/pixels'

/**
 * Whole-day items pinned above the scrolling grid (tech-stack §13): closures and all-day
 * time off. Always visible, never in the way of the morning hours.
 */
export function AllDayRow(props: { schedule: Schedule; columns: Column[] }) {
  const { schedule, columns } = props
  const zone = schedule.timezone
  const nameOf = (staffId: string) =>
    schedule.staff.find((s) => s.id === staffId)?.display_name ?? 'Someone'

  const chips = columns.map((column) => {
    const closures = schedule.closures
      .filter((c) => c.date === column.date)
      .map((c) => ({ key: `c-${c.id}`, text: `Closed · ${c.name}`, closed: true }))
    const absences = schedule.time_off
      .filter(
        (t) =>
          t.all_day &&
          (column.staffId === null || t.staff_id === column.staffId) &&
          localDate(new Date(t.starts_at), zone) <= column.date &&
          column.date < localDate(new Date(t.ends_at), zone),
      )
      .map((t) => ({
        key: `t-${t.id}`,
        text: [column.staffId === null ? nameOf(t.staff_id) : null, t.reason ?? 'Time off']
          .filter(Boolean)
          .join(' · '),
        closed: false,
      }))
    return [...closures, ...absences]
  })
  if (chips.every((list) => list.length === 0)) return null

  return (
    <div className="contents" data-testid="all-day-row">
      <div className="flex items-start justify-end border-b border-r bg-card px-2 py-1 text-xs text-muted-foreground">
        all day
      </div>
      {columns.map((column, i) => (
        <div
          key={column.key}
          className="flex min-h-7 flex-wrap gap-1 border-b border-r bg-card p-1"
          role="list"
          aria-label={`All day, ${column.label}`}
        >
          {chips[i].map((chip) => (
            <span
              key={chip.key}
              role="listitem"
              className={
                chip.closed
                  ? 'truncate rounded-sm bg-destructive/10 px-1.5 text-xs font-medium text-destructive'
                  : 'truncate rounded-sm bg-muted px-1.5 text-xs font-medium text-muted-foreground'
              }
            >
              {chip.text}
            </span>
          ))}
        </div>
      ))}
    </div>
  )
}
