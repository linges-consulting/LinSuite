import { instantAt } from '@/lib/calendar/pixels'

/** An instant as a wall-clock time in the business's zone (or the browser's, before the
 *  zone is known): `10:00 AM`. */
export function clock(instant: string | Date, zone?: string): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: 'numeric',
    minute: '2-digit',
    timeZone: zone,
  }).format(typeof instant === 'string' ? new Date(instant) : instant)
}

/** `minutes` past midnight on `date`, as a clock time. What a card prints while it is
 *  being dragged, before there is an instant to print. */
export function clockAt(date: string, minutes: number, zone: string): string {
  return clock(instantAt(date, minutes, zone), zone)
}

/** Today as `YYYY-MM-DD` on the business's calendar, whatever the browser's clock says. The
 *  `en-CA` locale is the one whose default date format *is* ISO. */
export function today(zone: string, now = new Date()): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: zone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(now)
}

/** `Mon 15` for a week column header. Built from the string so no zone is involved. */
export function weekdayLabel(date: string): { weekday: string; day: number } {
  const [y, m, d] = date.split('-').map(Number)
  const weekday = new Intl.DateTimeFormat('en', { weekday: 'short', timeZone: 'UTC' }).format(
    new Date(Date.UTC(y, m - 1, d)),
  )
  return { weekday, day: d }
}
