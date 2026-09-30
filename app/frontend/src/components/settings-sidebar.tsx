import { useState } from 'react'
import { ChevronDown, PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { SETTINGS_SECTIONS, type TabValue } from '@/lib/settings-nav'
import { cn } from '@/lib/utils'

const RAIL_KEY = 'linsuite.settings.rail-collapsed'
const SECTIONS_KEY = 'linsuite.settings.sections-open'

/** Per-device convenience, never load-bearing — a storage failure (private browsing, a full
 *  quota) just means the rail forgets its shape next visit, not that Settings breaks. */
function readRailCollapsed(): boolean {
  try {
    return localStorage.getItem(RAIL_KEY) === '1'
  } catch {
    return false
  }
}

function writeRailCollapsed(collapsed: boolean) {
  try {
    localStorage.setItem(RAIL_KEY, collapsed ? '1' : '0')
  } catch {
    // Storage denied or full — the toggle still works for this visit.
  }
}

function readSectionsOpen(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(SECTIONS_KEY)
    return raw ? JSON.parse(raw) : {}
  } catch {
    return {}
  }
}

function writeSectionsOpen(state: Record<string, boolean>) {
  try {
    localStorage.setItem(SECTIONS_KEY, JSON.stringify(state))
  } catch {
    // Same as above: this visit still works, it just won't remember next time.
  }
}

/**
 * Settings' own nested nav: a collapsible rail beside the panel, standing in for the line
 * tabs the page used to open on. `active`/`onSelect` mirror a controlled tab — the caller
 * (`SettingsPage`) still owns `?tab=`, this component only renders the picker.
 *
 * Two controls collapse independently and each remembers itself in `localStorage`, per
 * device: the whole rail (icons only, with tooltips) and each section. The active item's
 * section always renders open regardless of what was last saved — a deep link should never
 * land on a hidden item.
 *
 * Below `md` the rail gives way to a native `<select>` grouped by section: a real listbox
 * needs no JS to be a listbox, and one page doesn't need a second collapsible-sheet
 * implementation next to the app shell's own drawer.
 */
export function SettingsSidebar({
  active,
  onSelect,
}: {
  active: TabValue
  onSelect: (value: TabValue) => void
}) {
  const [collapsed, setCollapsed] = useState(readRailCollapsed)
  const [sectionsOpen, setSectionsOpen] = useState(readSectionsOpen)

  const toggleRail = () => {
    setCollapsed((prev) => {
      const next = !prev
      writeRailCollapsed(next)
      return next
    })
  }

  const setSectionOpen = (id: string, open: boolean) => {
    setSectionsOpen((prev) => {
      const next = { ...prev, [id]: open }
      writeSectionsOpen(next)
      return next
    })
  }

  return (
    <TooltipProvider>
      <nav
        aria-label="Settings"
        className={cn(
          'hidden shrink-0 flex-col gap-1 md:flex',
          collapsed ? 'w-11 items-center' : 'w-56',
        )}
      >
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={collapsed ? 'Expand settings sidebar' : 'Collapse settings sidebar'}
              onClick={toggleRail}
              className={cn('mb-1', !collapsed && 'self-end')}
            >
              {collapsed ? <PanelLeftOpen aria-hidden /> : <PanelLeftClose aria-hidden />}
            </Button>
          </TooltipTrigger>
          <TooltipContent side="right">
            {collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          </TooltipContent>
        </Tooltip>

        {SETTINGS_SECTIONS.map((section) => {
          const sectionHasActive = section.items.some((item) => item.value === active)

          if (collapsed) {
            return (
              <div key={section.id} className="flex flex-col gap-1">
                {section.items.map((item) => (
                  <Tooltip key={item.value}>
                    <TooltipTrigger asChild>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        aria-label={item.label}
                        aria-current={active === item.value ? 'page' : undefined}
                        onClick={() => onSelect(item.value)}
                        className={cn(
                          active === item.value && 'bg-sidebar-accent text-foreground',
                        )}
                      >
                        <item.icon className="size-4" aria-hidden />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent side="right">{item.label}</TooltipContent>
                  </Tooltip>
                ))}
              </div>
            )
          }

          const open = sectionHasActive || (sectionsOpen[section.id] ?? true)

          return (
            <Collapsible
              key={section.id}
              open={open}
              onOpenChange={(next) => setSectionOpen(section.id, next)}
            >
              <CollapsibleTrigger asChild>
                <button
                  type="button"
                  className="flex h-8 w-full items-center gap-1.5 rounded-md px-2 text-left text-xs font-medium text-muted-foreground transition-colors duration-150 hover:text-foreground"
                >
                  <ChevronDown
                    className={cn(
                      'size-3.5 shrink-0 transition-transform duration-150',
                      !open && '-rotate-90',
                    )}
                    aria-hidden
                  />
                  {section.label}
                </button>
              </CollapsibleTrigger>
              <CollapsibleContent className="flex flex-col gap-0.5 py-0.5 pl-2">
                {section.items.map((item) => (
                  <button
                    key={item.value}
                    type="button"
                    aria-current={active === item.value ? 'page' : undefined}
                    onClick={() => onSelect(item.value)}
                    className={cn(
                      'flex h-8 items-center gap-2.5 rounded-md px-2 text-sm font-medium text-muted-foreground transition-colors duration-150 hover:bg-sidebar-accent hover:text-foreground',
                      active === item.value && 'bg-sidebar-accent text-foreground',
                    )}
                  >
                    <item.icon className="size-4 shrink-0" aria-hidden />
                    {item.label}
                  </button>
                ))}
              </CollapsibleContent>
            </Collapsible>
          )
        })}
      </nav>

      {/* Narrow screens: one native listbox instead of a second drawer implementation —
          grouped exactly like the rail, no horizontal scroll possible. */}
      <label className="flex flex-col gap-1.5 md:hidden">
        <span className="text-xs font-medium text-muted-foreground">Settings section</span>
        <select
          value={active}
          onChange={(event) => onSelect(event.target.value as TabValue)}
          className="h-9 w-full rounded-lg border border-input bg-background px-2.5 text-sm"
        >
          {SETTINGS_SECTIONS.map((section) => (
            <optgroup key={section.id} label={section.label}>
              {section.items.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </label>
    </TooltipProvider>
  )
}
