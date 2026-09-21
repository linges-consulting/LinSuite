import type { Override } from '@/lib/api'

/**
 * The grid's own vocabulary. One engine, two column axes (tech-stack §13): a column is a
 * business-local date and, in the day view, a staff member; in the week view the staff
 * member is whoever the page filtered to, or nobody in particular.
 */
export type Column = {
  key: string
  date: string
  /** Null in the week view without a staff filter: every column carries everybody. */
  staffId: string | null
  label: string
  sublabel?: string
  today: boolean
  /** The column's person's concurrency limit; the header marks it when above one. */
  concurrency?: number
}

/** An in-progress move or resize, in minutes past midnight of the column's date. */
export type Drag = {
  id: string
  kind: 'move' | 'resize'
  column: number
  start: number
  end: number
}

/** A click-and-drag on empty space, before it becomes a booking. */
export type Selection = { column: number; start: number; end: number }

/** What the grid hands the booking dialog when somebody draws on it. */
export type Prefill = { staffId: string | null; date: string; startsAt: string }

export type Change = { starts_at?: string; duration_minutes?: number } & Override
