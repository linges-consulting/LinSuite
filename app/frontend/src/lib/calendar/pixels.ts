import { formatInTimeZone, fromZonedTime } from 'date-fns-tz'

/**
 * Time ↔ pixels, in the business's zone.
 *
 * The grid is 24 rows of exactly 60 px (tech-stack §13), so **one pixel is one wall-clock
 * minute** and a `y` is a time of day on the clock face. Wall-clock rather than elapsed
 * minutes since local midnight, deliberately: the 09:00 label has to sit on the line an
 * appointment at 09:00 starts on, on every day of the year. On the spring-forward day the
 * 02:00 row is an hour nobody can book (the server refuses those starts, `availability.py`);
 * on the fall-back day the two 01:30s land on the same line. Both are the honest picture
 * of a clock face — the alternative is a 23- or 25-row day, which is not the grid.
 *
 * ponytail: an event spanning the repeated hour draws an hour short on that one day a year.
 * Measure elapsed minutes instead if a business ever books through 01:00–02:00 in November.
 */

export const HOUR_PX = 60
export const DAY_PX = 24 * HOUR_PX
const MINUTES_IN_DAY = 24 * 60

/** Minutes past midnight on the clock in `zone` — `y` for an instant, fractional seconds kept. */
export function wallMinutes(instant: Date, zone: string): number {
  const [h, m, s] = formatInTimeZone(instant, zone, 'HH:mm:ss').split(':').map(Number)
  return h * 60 + m + s / 60
}

/** The local calendar date an instant falls on, `YYYY-MM-DD`. */
export function localDate(instant: Date, zone: string): string {
  return formatInTimeZone(instant, zone, 'yyyy-MM-dd')
}

/** The instant `minutes` past midnight names on `date` in `zone`. 1440 is the next
 *  midnight. A time the day does not have (02:30 on the spring-forward day) resolves an hour
 *  *on* — the way the server's `clock.localize` resolves it, so a drag to that line asks
 *  for the same instant the server would refuse, rather than quietly landing at 01:30. */
export function instantAt(date: string, minutes: number, zone: string): Date {
  const whole = Math.max(0, Math.min(MINUTES_IN_DAY, Math.round(minutes)))
  if (whole === MINUTES_IN_DAY) return instantAt(nextDate(date), 0, zone)
  const hh = String(Math.floor(whole / 60)).padStart(2, '0')
  const mm = String(whole % 60).padStart(2, '0')
  const instant = fromZonedTime(`${date}T${hh}:${mm}:00`, zone)
  // date-fns-tz reads a time inside the gap through the *pre*-transition offset and lands an
  // hour early; the round trip says by how much, and the difference pushes it forward.
  const short = whole - wallMinutes(instant, zone)
  return short > 0 ? new Date(instant.getTime() + short * 60_000) : instant
}

/** Calendar arithmetic on the string itself — no zone, no DST, no browser clock. */
export function addDays(date: string, days: number): string {
  const [y, m, d] = date.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10)
}

const nextDate = (date: string) => addDays(date, 1)

/**
 * Where an interval sits in the column for `date`: its top and height in pixels, clipped to
 * the day. An interval starting the day before begins at 0; one running past midnight ends
 * at `DAY_PX`. Null when none of it is on this date.
 */
export function placement(
  startsAt: Date,
  endsAt: Date,
  date: string,
  zone: string,
): { top: number; height: number } | null {
  const startDate = localDate(startsAt, zone)
  const endDate = localDate(endsAt, zone)
  if (startDate > date || endDate < date) return null
  const top = startDate < date ? 0 : wallMinutes(startsAt, zone)
  const bottom = endDate > date ? DAY_PX : wallMinutes(endsAt, zone)
  // An interval ending exactly at midnight ends at the bottom of the day before: on its
  // own date it is zero pixels tall, and that is the `null` here.
  if (bottom <= top) return null
  return { top, height: bottom - top }
}
