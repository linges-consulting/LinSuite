import { Users } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import type { RosterEntry } from '@/lib/api'
import { useTheme } from '@/lib/theme'

/**
 * The calendar toolbar's "Practitioners" control: which active practitioners' columns show,
 * with "Only me" and "All" shortcuts and a count so the trigger says what is chosen without
 * opening it. `routes/schedule.tsx` owns the selection (`lib/calendar/practitioners.ts`);
 * this only reads and asks for changes.
 */
export function PractitionersPicker(props: {
  practitioners: RosterEntry[]
  selected: Set<string>
  ownId: string | null
  onChange: (ids: string[]) => void
}) {
  const { practitioners, selected, ownId, onChange } = props
  const { resolvedTheme } = useTheme()

  const summary =
    practitioners.length > 0 && selected.size === practitioners.length
      ? 'All'
      : ownId !== null && selected.size === 1 && selected.has(ownId)
        ? 'Only me'
        : `${selected.size} selected`

  const toggle = (id: string, checked: boolean) => {
    const next = new Set(selected)
    if (checked) next.add(id)
    else next.delete(id)
    onChange([...next])
  }

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" className="gap-1.5">
          <Users aria-hidden />
          Practitioners
          <span className="text-muted-foreground">· {summary}</span>
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-64">
        <div className="flex gap-1.5">
          {ownId !== null && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="flex-1"
              onClick={() => onChange([ownId])}
            >
              Only me
            </Button>
          )}
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="flex-1"
            onClick={() => onChange(practitioners.map((m) => m.id))}
            disabled={practitioners.length === 0}
          >
            All
          </Button>
        </div>
        {practitioners.length === 0 ? (
          <p className="px-1.5 py-1 text-xs text-muted-foreground">No practitioners yet.</p>
        ) : (
          <ul className="flex flex-col gap-0.5">
            {practitioners.map((m) => (
              <li key={m.id}>
                <label className="flex items-center gap-2 rounded-md px-1.5 py-1 text-sm hover:bg-accent">
                  <Checkbox
                    checked={selected.has(m.id)}
                    onCheckedChange={(checked) => toggle(m.id, checked === true)}
                  />
                  <span
                    aria-hidden
                    className="size-2.5 shrink-0 rounded-full"
                    style={{ backgroundColor: resolvedTheme === 'dark' ? m.dark_hex : m.hex }}
                  />
                  <span className="truncate">{m.display_name}</span>
                </label>
              </li>
            ))}
          </ul>
        )}
      </PopoverContent>
    </Popover>
  )
}
