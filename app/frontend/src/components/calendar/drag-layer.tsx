import { EventBody, type Placed } from '@/components/calendar/event-card'
import type { Column, Drag, Selection } from '@/components/calendar/types'
import { clockAt } from '@/lib/calendar/format'

/**
 * What a drag looks like while it is happening, drawn in the column it is over: the ghost
 * of a moving or resizing card with its **live** time labels (tech-stack §13), or the range
 * being drawn on empty space. Nothing here is interactive; the sources of truth are the
 * drag state in `Grid` and the pointer.
 */
export function DragLayer(props: {
  columnIndex: number
  column: Column
  zone: string
  drag: Drag | null
  dragged: Placed | null
  selection: Selection | null
  colour: (staffId: string) => string
}) {
  const { drag, dragged, selection, column, zone } = props
  return (
    <>
      {drag && dragged && drag.column === props.columnIndex && (
        <EventBody
          {...dragged}
          top={drag.start}
          height={drag.end - drag.start}
          left={0}
          width={1}
          ghost
          className="pointer-events-none z-30"
          colour={props.colour(dragged.appointment.staff.id)}
          startLabel={clockAt(column.date, drag.start, zone)}
          endLabel={clockAt(column.date, drag.end, zone)}
        />
      )}
      {selection && selection.column === props.columnIndex && (
        <div
          className="pointer-events-none absolute inset-x-0.5 z-30 rounded-md border-2 border-primary bg-primary/15 px-1.5 py-0.5 text-xs font-medium tabular-nums text-foreground"
          style={{ top: selection.start, height: selection.end - selection.start }}
          data-testid="selection"
        >
          {clockAt(column.date, selection.start, zone)} –{' '}
          {clockAt(column.date, selection.end, zone)}
        </div>
      )}
    </>
  )
}
