import { useDraggable } from '@dnd-kit/core'
import type { ReactNode } from 'react'
import type { Appointment } from '@/lib/api'
import { readableOn } from '@/lib/calendar/contrast'
import { cn } from '@/lib/utils'

/**
 * One appointment on the grid (tech-stack §13): painted in its staff member's colour, a 3 px
 * stripe on the left, a radius of 10 % of its height, the title readable whatever the
 * colour. Even at fifteen minutes there is one clipped line.
 *
 * Buffers are drawn as a hatched extension above and below the body — the turnaround is
 * real time nobody else can have, but it is not the appointment, so it is not the card.
 *
 * `EventBody` is the look; `EventCard` is the look plus the two drags. The ghost that
 * follows a drag is an `EventBody` on its own, so nothing draggable is ever drawn twice.
 */

export type Placed = {
  appointment: Appointment
  top: number
  height: number
  /** Fraction of the column width, from the packing. */
  left: number
  width: number
}

type Look = {
  colour: string
  /** The two clock labels. Live during a drag: whatever the card is *about* to be. */
  startLabel: string
  endLabel: string
}

const HATCH = (colour: string) =>
  `repeating-linear-gradient(135deg, ${colour} 0 1px, transparent 1px 5px)`

export function EventBody(
  props: Placed & Look & { className?: string; ghost?: boolean; children?: ReactNode },
) {
  const { appointment: a, top, height, left, width, colour } = props
  const foreground = readableOn(colour)
  // A solid shade of the colour, pulled a third of the way toward the readable foreground:
  // darker on a light card, lighter on a dark one, visible on both themes.
  const stripe = `color-mix(in srgb, ${colour} 65%, ${foreground})`
  const title = `${a.customer.first_name} ${a.customer.last_name}`
  // Two lines need 36 px; under that there is one, clipped — a 15-minute card included.
  const tiny = height < 36
  return (
    <div
      className={cn('absolute', props.className)}
      style={{
        top,
        height,
        left: `calc(${left * 100}% + 1px)`,
        width: `calc(${width * 100}% - 2px)`,
      }}
    >
      {a.buffer_before_minutes > 0 && (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 opacity-30"
          style={{ top: -a.buffer_before_minutes, height: a.buffer_before_minutes, backgroundImage: HATCH(colour) }}
        />
      )}
      {a.buffer_after_minutes > 0 && (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 opacity-30"
          style={{ top: height, height: a.buffer_after_minutes, backgroundImage: HATCH(colour) }}
        />
      )}
      <div
        className={cn(
          'flex h-full flex-col overflow-hidden px-1.5 text-xs leading-tight',
          tiny ? 'justify-center' : 'py-1',
          props.ghost && 'ring-2 ring-ring ring-offset-1 ring-offset-background',
        )}
        style={{
          backgroundColor: colour,
          color: foreground,
          borderLeft: `3px solid ${stripe}`,
          borderRadius: height * 0.1,
        }}
      >
        {tiny ? (
          <span className="truncate font-medium">
            {title} · {props.startLabel}
          </span>
        ) : (
          <>
            <span className="truncate font-semibold">{title}</span>
            <span className="truncate tabular-nums opacity-90">
              {props.startLabel} – {props.endLabel}
            </span>
            {height >= 56 && <span className="truncate opacity-80">{a.service.name}</span>}
          </>
        )}
      </div>
      {props.children}
    </div>
  )
}

/** Only `confirmed` moves. Task 18's lifecycle adds the rest; the gate is here already. */
export const MOVABLE = new Set(['confirmed'])

export function EventCard(
  props: Placed & Look & { column: number; canManage: boolean; dragging: boolean },
) {
  const { appointment: a, column, top, height } = props
  const disabled = !props.canManage || !MOVABLE.has(a.status)
  const data = { appointment: a, column, start: top, end: top + height }
  const body = useDraggable({ id: `move:${a.id}`, data: { ...data, kind: 'move' }, disabled })
  const edge = useDraggable({
    id: `resize:${a.id}`,
    data: { ...data, kind: 'resize' },
    disabled,
  })
  const label = `${a.customer.first_name} ${a.customer.last_name}, ${a.service.name}, ${props.startLabel} to ${props.endLabel} with ${a.staff.display_name}`

  return (
    <EventBody
      {...props}
      className={cn(
        'group',
        props.dragging && 'opacity-40',
        a.status === 'cancelled' && 'opacity-50 line-through',
      )}
    >
      {/* oxlint-disable react/refs -- dnd-kit's `setNodeRef`/listeners are callbacks, not `.current` reads */}
      <div
        ref={body.setNodeRef}
        {...body.listeners}
        {...body.attributes}
        /* oxlint-enable react/refs */
        aria-label={label}
        data-testid={`event-${a.id}`}
        className={cn(
          'absolute inset-0 rounded-[inherit] outline-none focus-visible:ring-2 focus-visible:ring-ring',
          disabled ? 'cursor-default' : 'cursor-grab touch-none active:cursor-grabbing',
        )}
      />
      {!disabled && (
        /* oxlint-disable react/refs -- same: dnd-kit callbacks, no ref read in render */
        <div
          ref={edge.setNodeRef}
          {...edge.listeners}
          {...edge.attributes}
          /* oxlint-enable react/refs */
          aria-label={`Change the end of ${label}`}
          data-testid={`resize-${a.id}`}
          className="absolute inset-x-1 -bottom-1 h-2.5 cursor-ns-resize touch-none rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      )}
    </EventBody>
  )
}
