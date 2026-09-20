/**
 * Everything snaps to the business's grid (tech-stack §13: "all actions snap to 15-minute
 * increments"). The step comes from `granularity_minutes` on the schedule response — the
 * same number the server slides slots across — never a constant here.
 */

const MINUTES_IN_DAY = 24 * 60

/** The nearest grid time to `minutes`, kept inside the day. */
export function snap(minutes: number, step: number): number {
  return clamp(Math.round(minutes / step) * step, 0, MINUTES_IN_DAY)
}

/** A start moved by `delta` minutes, snapped, and kept where the whole `length` still fits
 *  inside the day. */
export function snapStart(start: number, delta: number, length: number, step: number): number {
  return clamp(snap(start + delta, step), 0, MINUTES_IN_DAY - length)
}

/** An end pulled by `delta` minutes, snapped, never closer than one step to `start`. */
export function snapEnd(start: number, end: number, delta: number, step: number): number {
  return clamp(snap(end + delta, step), start + step, MINUTES_IN_DAY)
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value))
}
