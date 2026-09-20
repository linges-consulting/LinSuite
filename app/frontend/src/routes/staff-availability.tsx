import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { Field, Form, FormError } from '@/components/form'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import {
  createTimeOff,
  deleteTimeOff,
  fetchStaffHours,
  fetchTimeOff,
  replaceStaffHours,
  type HoursBlock,
  type StaffRow,
  type TimeOffDraft,
  type TimeOffEntry,
} from '@/lib/api'
import {
  dayProblem,
  minutesToTime,
  nextBlock,
  STEP_SECONDS,
  timeToMinutes,
  WEEKDAYS,
  type Draft,
  type Week,
} from '@/lib/hours'
import { STAFF_HOURS, TIME_OFF } from '@/lib/query-keys'

/**
 * The two availability editors that hang off a staff row: the weekly matrix, and the list of
 * absences (PRD §1).
 *
 * They live here rather than in `settings-staff.tsx` because neither is about the person's
 * record — that screen edits who somebody is, these edit when they work — and one file
 * holding all three would be the longest in the app for no shared state.
 */

function toWeek(blocks: HoursBlock[]): Week {
  const week: Week = WEEKDAYS.map(() => [])
  for (const block of blocks) {
    week[block.weekday]?.push({
      start: minutesToTime(block.start_minute),
      end: minutesToTime(block.end_minute),
    })
  }
  return week
}

function toBlocks(week: Week): HoursBlock[] {
  return week.flatMap((day, weekday) =>
    day.map((block) => ({
      weekday,
      start_minute: timeToMinutes(block.start),
      end_minute: timeToMinutes(block.end, true),
    })),
  )
}

/**
 * The weekly matrix. Seven rows, each with zero or more blocks — several on one day *is* the
 * split shift, and the unavailable middle is the gap between them rather than a third block
 * saying "not working" (PRD §1).
 *
 * Saving sends the whole week. There is no per-block save, deliberately: an administrator
 * edits this as one thing, and a half-applied week is not a state worth being able to reach.
 */
export function HoursDialog({ member, onClose }: { member: StaffRow; onClose: () => void }) {
  const queryClient = useQueryClient()
  const hours = useQuery({
    queryKey: [...STAFF_HOURS, member.id],
    queryFn: () => fetchStaffHours(member.id),
  })
  const [week, setWeek] = useState<Week | null>(null)
  const current = week ?? (hours.data ? toWeek(hours.data) : null)

  const edit = (next: Week) => setWeek(next)
  const change = (weekday: number, index: number, field: keyof Draft, value: string) => {
    if (!current) return
    edit(
      current.map((day, d) =>
        d === weekday ? day.map((b, i) => (i === index ? { ...b, [field]: value } : b)) : day,
      ),
    )
  }
  const addBlock = (weekday: number) => {
    if (!current) return
    // Derived from where this day already ends — see `nextBlock`. A fixed afternoon draft
    // would land inside an existing 09:00–17:00 and warn about an overlap nobody made.
    const fresh = nextBlock(current[weekday])
    edit(current.map((day, d) => (d === weekday ? [...day, fresh] : day)))
  }
  const removeBlock = (weekday: number, index: number) => {
    if (!current) return
    edit(current.map((day, d) => (d === weekday ? day.filter((_, i) => i !== index) : day)))
  }

  const save = useMutation({
    mutationFn: () => replaceStaffHours(member.id, toBlocks(current ?? [])),
    onSuccess: () => {
      toast.success(`Saved ${member.display_name}'s hours`)
      queryClient.invalidateQueries({ queryKey: STAFF_HOURS })
      onClose()
    },
  })

  const problems = current ? current.map(dayProblem) : []
  const blocked = problems.some(Boolean)

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{member.display_name}&rsquo;s working hours</DialogTitle>
          <DialogDescription>
            Local times, kept as written across daylight saving. Add a second block to a day
            for a split shift — the gap between them is the break.
          </DialogDescription>
        </DialogHeader>

        {hours.isPending && <Skeleton className="h-64 w-full" />}
        {hours.isError && <FormError>{hours.error.message}</FormError>}

        {current && (
          <Form onSubmit={() => !blocked && save.mutate()}>
            <div className="flex flex-col divide-y divide-border">
              {WEEKDAYS.map((name, weekday) => (
                <div key={name} className="flex flex-col gap-2 py-3">
                  <div className="flex flex-wrap items-center gap-3">
                    <span className="w-24 shrink-0 text-sm font-medium">{name}</span>
                    <div className="flex flex-1 flex-wrap items-center gap-2">
                      {current[weekday].length === 0 && (
                        <span className="text-sm text-muted-foreground">Not working</span>
                      )}
                      {current[weekday].map((block, index) => (
                        // The index is the identity: blocks have no id until they are saved,
                        // and reordering is not something this editor offers.
                        // eslint-disable-next-line react/no-array-index-key
                        <div key={index} className="flex items-center gap-1.5">
                          <Input
                            type="time"
                            step={STEP_SECONDS}
                            className="w-36"
                            aria-label={`${name} block ${index + 1} start`}
                            value={block.start}
                            onChange={(e) => change(weekday, index, 'start', e.target.value)}
                          />
                          <span aria-hidden className="text-muted-foreground">
                            &ndash;
                          </span>
                          <Input
                            type="time"
                            step={STEP_SECONDS}
                            className="w-36"
                            aria-label={`${name} block ${index + 1} end`}
                            value={block.end}
                            onChange={(e) => change(weekday, index, 'end', e.target.value)}
                          />
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            aria-label={`Remove ${name} block ${index + 1}`}
                            onClick={() => removeBlock(weekday, index)}
                          >
                            <Trash2 aria-hidden />
                          </Button>
                        </div>
                      ))}
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        aria-label={`Add a block to ${name}`}
                        onClick={() => addBlock(weekday)}
                      >
                        <Plus aria-hidden />
                        Add block
                      </Button>
                    </div>
                  </div>
                  {problems[weekday] && (
                    <p role="alert" className="pl-27 text-xs text-destructive">
                      {problems[weekday]}
                    </p>
                  )}
                </div>
              ))}
            </div>

            {save.error && <FormError>{save.error.message}</FormError>}

            <DialogFooter>
              <Button type="button" variant="ghost" onClick={onClose}>
                Cancel
              </Button>
              <Button type="submit" disabled={save.isPending || blocked}>
                {save.isPending ? 'Saving…' : 'Save hours'}
              </Button>
            </DialogFooter>
          </Form>
        )}
      </DialogContent>
    </Dialog>
  )
}


/**
 * Time off and vacation: the absences that override the weekly matrix.
 *
 * All-day is a *date* range and inclusive of its last day — "away 1–5 July" means the fifth
 * as well. A timed absence is a local datetime range with no offset; the server converts both
 * with the business timezone, which is the only place that conversion happens.
 */
export function TimeOffDialog({ member, onClose }: { member: StaffRow; onClose: () => void }) {
  const queryClient = useQueryClient()
  const entries = useQuery({
    queryKey: [...TIME_OFF, member.id],
    queryFn: () => fetchTimeOff(member.id),
  })
  const refresh = () => queryClient.invalidateQueries({ queryKey: TIME_OFF })

  const [allDay, setAllDay] = useState(true)
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [startsAt, setStartsAt] = useState('')
  const [endsAt, setEndsAt] = useState('')
  const [reason, setReason] = useState('')

  const draft: TimeOffDraft = allDay
    ? {
        all_day: true,
        start_date: startDate,
        // Absent means the one day. Sending the start again would say the same thing twice.
        ...(endDate ? { end_date: endDate } : {}),
        reason: reason.trim() || null,
      }
    : {
        all_day: false,
        // No offset, ever: the server reads these against the business timezone and refuses
        // anything carrying one.
        starts_at_local: startsAt,
        ends_at_local: endsAt,
        reason: reason.trim() || null,
      }

  const add = useMutation({
    mutationFn: () => createTimeOff(member.id, draft),
    onSuccess: () => {
      toast.success(`Added time off for ${member.display_name}`)
      setStartDate('')
      setEndDate('')
      setStartsAt('')
      setEndsAt('')
      setReason('')
      refresh()
    },
  })
  const remove = useMutation({
    mutationFn: (id: string) => deleteTimeOff(member.id, id),
    onSuccess: () => {
      toast.success('Removed the time off')
      refresh()
    },
    onError: (error: Error) => toast.error(error.message),
  })

  const incomplete = allDay ? !startDate : !startsAt || !endsAt

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{member.display_name}&rsquo;s time off</DialogTitle>
          <DialogDescription>
            Vacation, appointments and days owed. These override the weekly hours.
          </DialogDescription>
        </DialogHeader>

        {entries.isPending && <Skeleton className="h-24 w-full" />}
        {entries.isError && <FormError>{entries.error.message}</FormError>}
        {entries.data && (
          <ul className="flex flex-col divide-y divide-border">
            {entries.data.length === 0 && (
              <li className="py-3 text-sm text-muted-foreground">Nothing booked.</li>
            )}
            {entries.data.map((entry) => (
              <li key={entry.id} className="flex items-center justify-between gap-3 py-2.5">
                <div className="flex flex-col gap-0.5">
                  <span className="text-sm font-medium">{describe(entry)}</span>
                  {entry.reason && (
                    <span className="text-xs text-muted-foreground">{entry.reason}</span>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <Badge variant={entry.all_day ? 'secondary' : 'info'}>
                    {entry.all_day ? 'All day' : 'Timed'}
                  </Badge>
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`Remove ${describe(entry)}`}
                    disabled={remove.isPending}
                    onClick={() => remove.mutate(entry.id)}
                  >
                    <Trash2 aria-hidden />
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}

        <Form onSubmit={() => !incomplete && add.mutate()}>
          <div className="flex items-center gap-2">
            <Checkbox
              id="time-off-all-day"
              checked={allDay}
              onCheckedChange={(on) => setAllDay(on === true)}
            />
            <Label htmlFor="time-off-all-day" className="font-normal">
              All day
            </Label>
          </div>

          {allDay ? (
            <div className="grid gap-5 sm:grid-cols-2">
              <Field label="First day" htmlFor="time-off-start-date">
                <Input
                  id="time-off-start-date"
                  type="date"
                  required
                  value={startDate}
                  onChange={(e) => setStartDate(e.target.value)}
                />
              </Field>
              <Field
                label="Last day"
                htmlFor="time-off-end-date"
                hint="Inclusive. Leave blank for a single day."
              >
                <Input
                  id="time-off-end-date"
                  type="date"
                  min={startDate || undefined}
                  value={endDate}
                  onChange={(e) => setEndDate(e.target.value)}
                />
              </Field>
            </div>
          ) : (
            <div className="grid gap-5 sm:grid-cols-2">
              <Field label="From" htmlFor="time-off-starts-at">
                <Input
                  id="time-off-starts-at"
                  type="datetime-local"
                  required
                  value={startsAt}
                  onChange={(e) => setStartsAt(e.target.value)}
                />
              </Field>
              <Field label="Until" htmlFor="time-off-ends-at">
                <Input
                  id="time-off-ends-at"
                  type="datetime-local"
                  required
                  min={startsAt || undefined}
                  value={endsAt}
                  onChange={(e) => setEndsAt(e.target.value)}
                />
              </Field>
            </div>
          )}

          <Field label="Reason" htmlFor="time-off-reason" hint="Optional.">
            <Input
              id="time-off-reason"
              maxLength={200}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>

          {add.error && <FormError>{add.error.message}</FormError>}

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>
              Done
            </Button>
            <Button type="submit" disabled={add.isPending || incomplete}>
              {add.isPending ? 'Adding…' : 'Add time off'}
            </Button>
          </DialogFooter>
        </Form>
      </DialogContent>
    </Dialog>
  )
}

/** What an entry reads as in the list. The server sends the local halves, so this never has
 *  to work out which local day a UTC instant belongs to. */
function describe(entry: TimeOffEntry): string {
  if (entry.all_day) {
    return entry.start_date === entry.end_date
      ? entry.start_date
      : `${entry.start_date} – ${entry.end_date}`
  }
  return `${entry.starts_at_local.replace('T', ' ').slice(0, 16)} – ${entry.ends_at_local
    .replace('T', ' ')
    .slice(0, 16)}`
}
