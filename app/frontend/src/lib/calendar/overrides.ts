import type { Appointment, OverrideRule, Schedule, User } from '@/lib/api'
import { localDate } from '@/lib/calendar/pixels'

/**
 * The words for an availability override (tech-stack §22): what a broken rule means in a
 * sentence, who may set it aside, and how an overridden appointment's marker reads. Pure,
 * beside the rest of the calendar's arithmetic, and pinned by `tests/schedule.test.tsx`.
 */

export const OVERRIDE_CAPABILITY = 'schedule.override_availability'

export type Situation = {
  staffId: string
  name: string
  startsAt: string
  endsAt: string
}

/** One plain sentence per broken rule, in the server's order. */
export function describeRules(rules: OverrideRule[], where: Situation, schedule: Schedule): string[] {
  const zone = schedule.timezone
  const start = new Date(where.startsAt)
  const end = new Date(where.endsAt)
  const date = localDate(start, zone)
  return rules.map((rule) => {
    switch (rule) {
      case 'outside_shift': {
        const blocks = schedule.working_blocks.filter((b) => b.staff_id === where.staffId && b.date === date)
        if (blocks.length === 0) return `${where.name} is not scheduled to work that day`
        const shiftEnd = Math.max(...blocks.map((b) => new Date(b.ends_at).getTime()))
        const shiftStart = Math.min(...blocks.map((b) => new Date(b.starts_at).getTime()))
        if (end.getTime() > shiftEnd) {
          return `This runs ${spell(end.getTime() - shiftEnd)} past ${where.name}'s shift end`
        }
        if (start.getTime() < shiftStart) {
          return `This starts ${spell(shiftStart - start.getTime())} before ${where.name}'s shift`
        }
        return `This falls outside ${where.name}'s shift`
      }
      case 'time_off': {
        const off = schedule.time_off.find(
          (t) =>
            t.staff_id === where.staffId &&
            new Date(t.starts_at).getTime() < end.getTime() &&
            new Date(t.ends_at).getTime() > start.getTime(),
        )
        const when = off && !off.all_day ? 'then' : 'that day'
        return `${where.name} is on time off ${when}${off?.reason ? ` (${off.reason})` : ''}`
      }
      case 'closure': {
        const closure = schedule.closures.find((c) => c.date === date)
        return `The business is closed that day${closure ? ` (${closure.name})` : ''}`
      }
      case 'beyond_horizon':
        return 'That date is beyond the booking window'
      default:
        return `This breaks the rule "${rule}"`
    }
  })
}

/** "30 minutes", "1 hour", "1 h 15 min". */
function spell(ms: number): string {
  const minutes = Math.round(ms / 60_000)
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  if (hours === 0) return `${minutes} minute${minutes === 1 ? '' : 's'}`
  if (rest === 0) return `${hours} hour${hours === 1 ? '' : 's'}`
  return `${hours} h ${rest} min`
}

/**
 * Why this session may not confirm for `staffId`, or null when it may: the capability for
 * one's own schedule; `admin`, in Admin Mode, for anybody else's. Mirrors the server's
 * `authorize_override`, which is the one that counts.
 */
export function whyNotOverride(user: User | null, ownStaffId: string | null, staffId: string): string | null {
  if (!user?.capabilities.includes(OVERRIDE_CAPABILITY)) {
    return 'Booking outside availability needs the "override availability" permission, which your role does not hold. Ask an administrator.'
  }
  if (staffId === ownStaffId) return null
  if (!user.capabilities.includes('admin')) {
    return "Only an administrator can book outside another staff member's availability. You can book outside your own."
  }
  if (user.mode !== 'admin') {
    return "Switch to Admin Mode to book outside another staff member's availability."
  }
  return null
}

/** The rule names as a marker's tooltip says them. */
export const RULE_LABEL: Record<OverrideRule, string> = {
  outside_shift: 'outside the shift',
  time_off: 'on time off',
  closure: 'on a closed day',
  beyond_horizon: 'beyond the booking window',
}

/** "Booked outside availability: outside the shift — Client asked", or null. */
export function overrideSummary(a: Appointment): string | null {
  if (!a.overridden_rules?.length) return null
  const rules = a.overridden_rules.map((r) => RULE_LABEL[r] ?? r).join(', ')
  return `Booked outside availability: ${rules}${a.override_reason ? ` — ${a.override_reason}` : ''}`
}
