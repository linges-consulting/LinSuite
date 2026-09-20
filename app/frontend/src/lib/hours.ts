/**
 * The weekly matrix's arithmetic, away from its markup.
 *
 * Working hours are **minutes since local midnight** — the server stores them that way
 * because "works Mondays 09:00–12:00" is a rule about a clock face and not a UTC instant
 * (PRD §1, CLAUDE.md "Time"). An `<input type="time">` speaks `HH:MM`, so these two
 * functions are the whole of the translation, and `dayProblem` is the courtesy check the
 * screen runs before the round trip that the database's exclusion constraint enforces
 * regardless.
 */

/** One block as the editor holds it, before it becomes minutes. */
export type Draft = { start: string; end: string }
export type Week = Draft[][]

/** ISO order, Monday = 0, which is what the server stores and what `Date#getDay` is not. */
export const WEEKDAYS = [
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
  'Sunday',
]

/** The step the server enforces, in the seconds an `<input type="time">` wants. Five
 *  minutes, because a shift that starts at 09:07 is a typo. */
export const STEP_SECONDS = 300
const MINUTES_IN_DAY = 1440

const pad = (n: number) => String(n).padStart(2, '0')

/** Minutes since local midnight → what an `<input type="time">` displays. 1440 renders as
 *  `00:00`, which is the one value that reads as midnight at either end of a day. */
export function minutesToTime(minute: number): string {
  return `${pad(Math.floor(minute / 60) % 24)}:${pad(minute % 60)}`
}

/**
 * The reverse. `00:00` in an *end* field is midnight at the end of the day — 1440, the value
 * the server allows there — rather than a block of zero length. A time input cannot type
 * `24:00`, and a business whose evening shift runs to midnight is ordinary enough that
 * refusing it would be the wrong trade.
 */
export function timeToMinutes(value: string, asEnd = false): number {
  const [hours, minutes] = value.split(':').map(Number)
  const total = (hours || 0) * 60 + (minutes || 0)
  return asEnd && total === 0 ? MINUTES_IN_DAY : total
}

/** What is wrong with one day's blocks, if anything. Said before the round trip rather than
 *  instead of it — the exclusion constraint in the database is the enforcement. */
export function dayProblem(blocks: Draft[]): string | null {
  const spans = blocks.map((b) => ({
    start: timeToMinutes(b.start),
    end: timeToMinutes(b.end, true),
  }))
  if (spans.some((s) => s.start >= s.end)) return 'A block has to end after it starts.'
  const sorted = [...spans].sort((a, b) => a.start - b.start)
  if (sorted.some((s, i) => i > 0 && s.start < sorted[i - 1].end)) {
    return 'These blocks overlap. Leave a gap between them for the break.'
  }
  return null
}

/** A first block: the ordinary working day, for a weekday that had none. */
const FIRST: Draft = { start: '09:00', end: '17:00' }
/** The gap a split shift leaves, and how long the afternoon half starts out. */
const BREAK_MINUTES = 60
const SECOND_BLOCK_MINUTES = 180

/**
 * The block "Add block" should insert into a day that already has some.
 *
 * Derived from where the day currently ends rather than fixed, because a fixed afternoon
 * draft is only ever right for one shape of day: dropped onto somebody working 09:00–17:00 it
 * lands *inside* their shift, and the editor greets the click with an overlap warning the
 * person did not cause. Starting a break after the latest end means the common case — set
 * 09:00–12:00, press Add — produces 13:00–16:00 and no warning at all.
 *
 * `end` is the latest in the day rather than the last in the array: nothing sorts these, and
 * a day edited out of order would otherwise stack a new block on top of an earlier one.
 */
export function nextBlock(day: Draft[]): Draft {
  if (day.length === 0) return FIRST
  const latest = Math.max(...day.map((b) => timeToMinutes(b.end, true)))
  // Never past midnight. A day already running to 24:00 has nowhere to put another block, and
  // the overlap warning saying so is more honest than silently refusing the click.
  const start = Math.min(latest + BREAK_MINUTES, MINUTES_IN_DAY - STEP_SECONDS / 60)
  return {
    start: minutesToTime(start),
    end: minutesToTime(Math.min(start + SECOND_BLOCK_MINUTES, MINUTES_IN_DAY)),
  }
}

