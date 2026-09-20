import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragMoveEvent,
  type DragStartEvent,
  type KeyboardCoordinateGetter,
} from '@dnd-kit/core'
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent } from 'react'
import { AllDayRow } from '@/components/calendar/all-day-row'
import { DragLayer } from '@/components/calendar/drag-layer'
import { EventCard, type Placed } from '@/components/calendar/event-card'
import { NowLine } from '@/components/calendar/now-line'
import { TimeGutter } from '@/components/calendar/time-gutter'
import type { Change, Column, Drag, Prefill, Selection } from '@/components/calendar/types'
import type { Appointment, Schedule } from '@/lib/api'
import { clock, clockAt } from '@/lib/calendar/format'
import { pack } from '@/lib/calendar/packing'
import { DAY_PX, HOUR_PX, instantAt, localDate, placement, wallMinutes } from '@/lib/calendar/pixels'
import { snapEnd, snapStart } from '@/lib/calendar/snap'
import { useTheme } from '@/lib/theme'
import { cn } from '@/lib/utils'

/**
 * The grid (tech-stack §13, binding): 24 rows of exactly 60 px, columns from the page —
 * staff members in the day view, dates in the week view — and every rule the same on both.
 *
 * **Everything is in minutes past midnight of the column's date**, one pixel each, and only
 * becomes an instant at the edges: `placement` turns the server's instants into pixels on
 * the way in, `instantAt` turns the drop back into one on the way out. Both go through the
 * business zone (`lib/calendar/pixels.ts`), never the browser's.
 *
 * **Three interactions, two mechanisms.** Moving (drag the body) and resizing (drag the
 * bottom edge, only) are dnd-kit drags with a pointer sensor and a keyboard sensor: arrows
 * step one grid unit, Shift+arrows pull the end instead, Enter drops, Escape puts it back.
 * Drawing on empty space is plain pointer capture on the column, because there is nothing
 * to pick up. All three snap to the grid the server slides slots across.
 *
 * **The drag is a preview, not a change.** The card stays where it was, dimmed, and a ghost
 * with live times follows the pointer; on drop the page is handed the change and decides
 * what to do with it (optimistically, with a rollback — `routes/schedule.tsx`). Nothing here
 * writes.
 */

export function Grid(props: {
  schedule: Schedule
  columns: Column[]
  canManage: boolean
  onCreate: (prefill: Prefill) => void
  onChange: (appointment: Appointment, change: Change) => void
}) {
  const { schedule, columns, canManage } = props
  const zone = schedule.timezone
  const step = schedule.granularity_minutes
  const { resolvedTheme } = useTheme()
  const colour = (staffId: string) => {
    const member = schedule.staff.find((s) => s.id === staffId)
    if (!member) return resolvedTheme === 'dark' ? '#94a3b8' : '#475569'
    return resolvedTheme === 'dark' ? member.dark_hex : member.hex
  }

  const now = useNow()
  const todayDate = localDate(now, zone)
  const nowMinutes = wallMinutes(now, zone)
  const anyToday = columns.some((c) => c.date === todayDate)

  const laid = useMemo(
    () =>
      columns.map((column) => {
        const here: Placed[] = []
        for (const a of schedule.appointments) {
          if (column.staffId !== null && a.staff.id !== column.staffId) continue
          const box = placement(new Date(a.starts_at), new Date(a.ends_at), column.date, zone)
          if (box) here.push({ appointment: a, ...box, left: 0, width: 1 })
        }
        const packed = pack(here.map((p) => ({ id: p.appointment.id, start: p.top, end: p.top + p.height })))
        return here.map((p) => {
          const place = packed.get(p.appointment.id)!
          return { ...p, left: place.column / place.columns, width: 1 / place.columns }
        })
      }),
    [columns, schedule.appointments, zone],
  )

  // --- move and resize ------------------------------------------------------------------

  const [drag, setDragState] = useState<Drag | null>(null)
  const dragRef = useRef<Drag | null>(null)
  const setDrag = (next: Drag | null) => {
    dragRef.current = next
    setDragState(next)
  }
  const body = useRef<HTMLDivElement>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const header = useRef<HTMLDivElement>(null)
  const columnWidth = () =>
    body.current?.querySelector<HTMLElement>('[data-column]')?.getBoundingClientRect().width || 1
  // Sideways is across dates, never across people: a move changes when, not who (the
  // PATCH carries no staff), so in the day view a card stays in its column.
  const clampColumn = (from: number, to: number) => {
    const i = Math.max(0, Math.min(columns.length - 1, to))
    return columns[i].staffId === columns[from].staffId ? i : from
  }

  /** Keep the ghost in the scroll box while the keyboard walks it around. */
  const reveal = (next: Drag) => {
    const el = scroller.current
    if (!el) return
    const offset = header.current?.offsetHeight ?? 0
    const top = offset + next.start
    const bottom = offset + next.end
    if (top < el.scrollTop + offset + HOUR_PX) el.scrollTop = top - offset - HOUR_PX
    else if (bottom > el.scrollTop + el.clientHeight - HOUR_PX) {
      el.scrollTop = bottom - el.clientHeight + HOUR_PX
    }
  }

  // The keyboard steps the drag state directly and hands dnd-kit nothing to move: its own
  // coordinate arithmetic assumes the node follows the transform, and here it never does —
  // the card stays put and a ghost shows the answer. dnd-kit still owns pick up (Enter or
  // Space), drop (again) and cancel (Escape).
  const keyboard: KeyboardCoordinateGetter = (event) => {
    const current = dragRef.current
    const direction = ARROWS[event.code]
    if (!current || !direction) return undefined
    event.preventDefault()
    const [dx, dy] = direction
    const length = current.end - current.start
    let next: Drag
    if (current.kind === 'resize' || event.shiftKey) {
      next = { ...current, end: snapEnd(current.start, current.end, dy * step, step) }
    } else {
      const start = snapStart(current.start, dy * step, length, step)
      const column = clampColumn(current.column, current.column + dx)
      next = { ...current, start, end: start + length, column }
    }
    setDrag(next)
    reveal(next)
    return undefined
  }
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: keyboard }),
  )

  const onDragStart = (event: DragStartEvent) => {
    const data = event.active.data.current as DragData
    setDrag({
      id: data.appointment.id,
      kind: data.kind,
      column: data.column,
      start: data.start,
      end: data.end,
    })
  }

  const onDragMove = (event: DragMoveEvent) => {
    const current = dragRef.current
    if (!current) return
    // A keyboard drag is stepped in `keyboard` above; the moves dnd-kit reports for it are
    // its scroll bookkeeping, not steps.
    if (event.activatorEvent instanceof KeyboardEvent) return
    const origin = event.active.data.current as DragData
    const { delta } = event
    const length = current.end - current.start
    if (current.kind === 'resize') {
      setDrag({ ...current, end: snapEnd(origin.start, origin.end, delta.y, step) })
    } else {
      const start = snapStart(origin.start, delta.y, origin.end - origin.start, step)
      const column = clampColumn(origin.column, origin.column + Math.round(delta.x / columnWidth()))
      setDrag({ ...current, start, end: start + length, column })
    }
  }

  const onDragEnd = (event: DragEndEvent) => {
    const final = dragRef.current
    setDrag(null)
    if (!final) return
    const { appointment } = event.active.data.current as DragData
    const startsAt = instantAt(columns[final.column].date, final.start, zone)
    const change: Change = {}
    if (startsAt.getTime() !== new Date(appointment.starts_at).getTime()) {
      change.starts_at = startsAt.toISOString()
    }
    if (final.end - final.start !== appointment.duration_minutes) {
      change.duration_minutes = final.end - final.start
    }
    if (Object.keys(change).length > 0) props.onChange(appointment, change)
  }

  const dragged = drag
    ? (laid.flat().find((p) => p.appointment.id === drag.id) ?? null)
    : null

  // --- drawing on empty space -------------------------------------------------------------

  const [selection, setSelectionState] = useState<Selection | null>(null)
  const selectionRef = useRef<Selection | null>(null)
  const anchor = useRef<number | null>(null)
  const setSelection = (next: Selection | null) => {
    selectionRef.current = next
    setSelectionState(next)
  }
  const slotAt = (event: PointerEvent<HTMLDivElement>) => {
    const y = event.clientY - event.currentTarget.getBoundingClientRect().top
    return Math.max(0, Math.min(DAY_PX - step, Math.floor(y / step) * step))
  }
  const draw = {
    onPointerDown: (i: number) => (event: PointerEvent<HTMLDivElement>) => {
      if (!canManage || event.button !== 0 || event.target !== event.currentTarget) return
      if (event.pointerType === 'touch') return
      const slot = slotAt(event)
      anchor.current = slot
      setSelection({ column: i, start: slot, end: slot + step })
      event.currentTarget.setPointerCapture(event.pointerId)
    },
    onPointerMove: (i: number) => (event: PointerEvent<HTMLDivElement>) => {
      if (anchor.current === null) return
      const slot = slotAt(event)
      setSelection({
        column: i,
        start: Math.min(anchor.current, slot),
        end: Math.max(anchor.current, slot) + step,
      })
    },
    onPointerUp: () => {
      const drawn = selectionRef.current
      anchor.current = null
      setSelection(null)
      if (!drawn) return
      const column = columns[drawn.column]
      props.onCreate({
        staffId: column.staffId,
        date: column.date,
        startsAt: instantAt(column.date, drawn.start, zone).toISOString(),
      })
    },
    onPointerCancel: () => {
      anchor.current = null
      setSelection(null)
    },
  }

  // --- the first scroll lands on now ------------------------------------------------------

  useLayoutEffect(() => {
    const el = scroller.current
    if (!el) return
    const firstShift = Math.min(
      ...schedule.working_blocks
        .filter((b) => columns.some((c) => c.date === b.date))
        .map((b) => wallMinutes(new Date(b.starts_at), zone)),
    )
    const target = anyToday ? nowMinutes : Number.isFinite(firstShift) ? firstShift : 8 * HOUR_PX
    const headerHeight = header.current?.offsetHeight ?? 0
    el.scrollTop = Math.max(0, headerHeight + target - el.clientHeight / 3)
    // Once, on mount: stepping days keeps the scroll where the person left it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const template = `4.25rem repeat(${columns.length}, minmax(9rem, 1fr))`
  const announce = (id: string) => {
    const current = dragRef.current
    if (!current) return ''
    const column = columns[current.column]
    return `${clockAt(column.date, current.start, zone)} to ${clockAt(column.date, current.end, zone)}, ${column.label}. ${id}`
  }

  return (
    <DndContext
      sensors={sensors}
      onDragStart={onDragStart}
      onDragMove={onDragMove}
      onDragEnd={onDragEnd}
      onDragCancel={() => setDrag(null)}
      accessibility={{
        announcements: {
          onDragStart: () => 'Picked up. Arrows move by one step, Shift and arrows change the end, Enter drops, Escape cancels.',
          onDragMove: ({ active }) => announce(String(active.id)),
          onDragOver: () => undefined,
          onDragEnd: ({ active }) => `Dropped at ${announce(String(active.id))}`,
          onDragCancel: () => 'Put back.',
        },
      }}
    >
      <div
        ref={scroller}
        className="relative overflow-auto rounded-xl border bg-card"
        style={{ height: 'calc(100dvh - 11.5rem)', minHeight: '24rem' }}
        data-testid="grid"
      >
        <div
          ref={header}
          className="sticky top-0 z-40 grid bg-card"
          style={{ gridTemplateColumns: template }}
        >
          <div className="border-b border-r bg-card" />
          {columns.map((column) => (
            <div
              key={column.key}
              className={cn(
                'flex h-10 items-center gap-2 truncate border-b border-r bg-card px-2 text-sm font-medium',
                // Today stands out among dates; among people every column is today.
                column.today && column.staffId === null && 'text-primary',
              )}
              role="columnheader"
            >
              {column.staffId && (
                <span
                  aria-hidden
                  className="size-2.5 shrink-0 rounded-full"
                  style={{ backgroundColor: colour(column.staffId) }}
                />
              )}
              <span className="truncate">{column.label}</span>
              {column.sublabel && (
                <span className="text-xs font-normal text-muted-foreground">{column.sublabel}</span>
              )}
            </div>
          ))}
          <AllDayRow schedule={schedule} columns={columns} />
        </div>

        <div ref={body} className="grid" style={{ gridTemplateColumns: template, height: DAY_PX }}>
          <TimeGutter
            nowMinutes={anyToday ? nowMinutes : null}
            nowLabel={clock(now, zone)}
          />
          {columns.map((column, i) => {
            // A closure, or the column's person away all day: nothing to light up.
            const closed =
              schedule.closures.some((c) => c.date === column.date) ||
              schedule.time_off.some(
                (t) =>
                  t.all_day &&
                  t.staff_id === column.staffId &&
                  localDate(new Date(t.starts_at), zone) <= column.date &&
                  column.date < localDate(new Date(t.ends_at), zone),
              )
            const shifts = closed
              ? []
              : schedule.working_blocks.filter(
                  (b) =>
                    b.date === column.date && (column.staffId === null || b.staff_id === column.staffId),
                )
            const absences =
              column.staffId === null
                ? []
                : schedule.time_off.filter((t) => !t.all_day && t.staff_id === column.staffId)
            return (
              <div
                key={column.key}
                data-column={column.key}
                data-testid={`column-${column.key}`}
                role="gridcell"
                aria-label={column.label}
                className={cn(
                  'relative border-r bg-muted/60 select-none',
                  canManage && 'cursor-crosshair',
                )}
                onPointerDown={draw.onPointerDown(i)}
                onPointerMove={draw.onPointerMove(i)}
                onPointerUp={draw.onPointerUp}
                onPointerCancel={draw.onPointerCancel}
              >
                {/* Shifts: the working hours, lit. Everything outside them stays dimmed. */}
                {shifts.map((b) => {
                  const box = placement(new Date(b.starts_at), new Date(b.ends_at), column.date, zone)
                  return (
                    box && (
                      <div
                        key={`${b.staff_id}-${b.starts_at}`}
                        className="pointer-events-none absolute inset-x-0 bg-card"
                        style={{ top: box.top, height: box.height }}
                        data-testid="shift"
                      />
                    )
                  )
                })}
                {/* Hour lines, with a fainter one on the half hour. */}
                <div
                  aria-hidden
                  className="pointer-events-none absolute inset-0"
                  style={{
                    backgroundImage: `repeating-linear-gradient(to bottom, var(--border) 0 1px, transparent 1px ${HOUR_PX}px), repeating-linear-gradient(to bottom, transparent 0 ${HOUR_PX / 2}px, color-mix(in srgb, var(--border) 50%, transparent) ${HOUR_PX / 2}px ${HOUR_PX / 2 + 1}px, transparent ${HOUR_PX / 2 + 1}px ${HOUR_PX}px)`,
                  }}
                />
                {/* Timed time off, hatched. */}
                {absences.map((t) => {
                  const box = placement(new Date(t.starts_at), new Date(t.ends_at), column.date, zone)
                  return (
                    box && (
                      <div
                        key={t.id}
                        className="pointer-events-none absolute inset-x-0 overflow-hidden px-1.5 py-0.5 text-xs text-muted-foreground"
                        style={{
                          top: box.top,
                          height: box.height,
                          backgroundImage:
                            'repeating-linear-gradient(135deg, color-mix(in srgb, var(--muted-foreground) 35%, transparent) 0 1px, transparent 1px 7px)',
                        }}
                        data-testid="time-off"
                      >
                        <span className="truncate">{t.reason ?? 'Time off'}</span>
                      </div>
                    )
                  )
                })}
                {column.date === todayDate && <NowLine minutes={nowMinutes} />}
                {laid[i].map((p) => (
                  <EventCard
                    key={p.appointment.id}
                    {...p}
                    column={i}
                    canManage={canManage}
                    dragging={drag?.id === p.appointment.id}
                    colour={colour(p.appointment.staff.id)}
                    startLabel={clock(p.appointment.starts_at, zone)}
                    endLabel={clock(p.appointment.ends_at, zone)}
                  />
                ))}
                <DragLayer
                  columnIndex={i}
                  column={column}
                  zone={zone}
                  drag={drag}
                  dragged={dragged}
                  selection={selection}
                  colour={colour}
                />
              </div>
            )
          })}
        </div>
      </div>
    </DndContext>
  )
}

const ARROWS: Record<string, [number, number]> = {
  ArrowUp: [0, -1],
  ArrowDown: [0, 1],
  ArrowLeft: [-1, 0],
  ArrowRight: [1, 0],
}

type DragData = {
  appointment: Appointment
  kind: 'move' | 'resize'
  column: number
  start: number
  end: number
}

/** The clock, ticking once a minute — enough for a line that is one pixel per minute. */
function useNow(): Date {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 60_000)
    return () => clearInterval(id)
  }, [])
  return now
}
