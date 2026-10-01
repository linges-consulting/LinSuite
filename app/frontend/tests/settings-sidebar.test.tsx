import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Toaster } from '@/components/ui/sonner'
import { createQueryClient } from '@/lib/query-client'
import { SETTINGS_SECTIONS } from '@/lib/settings-nav'
import { ThemeProvider } from '@/lib/theme'
import { SettingsPage } from '@/routes/settings'

/**
 * The Settings sidebar (#122's line tabs replaced by a collapsible rail): every panel this
 * page can open on must still be one click away, `?tab=` must still deep-link, and the two
 * independent collapse states (whole rail, each section) must remember themselves per
 * device.
 *
 * Panels are real, but nothing here asserts their fetched content — only that a value
 * appears in the sidebar, that selecting it is reflected in `aria-current`, and that
 * `?tab=` still drives which one shows. Individual panels already have their own fetch-level
 * coverage (`resources.test.tsx`, `staff.test.tsx`, ...); this file is about the nav that
 * gets you to them.
 */

// The panels themselves fetch on mount; none of that is under test here. Every list endpoint
// in this app (`fetchStaff`, `fetchRoles`, `fetchClosures`, `fetchNoteTemplates`, ...) is
// typed as returning a bare array, so answering everything with `[]` is a real, tolerated
// shape rather than a guess — panels render their empty state instead of crashing.
function stubFetch() {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json([])))
}

function renderSettings(path = '/settings') {
  return render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter initialEntries={[path]}>
          <SettingsPage />
        </MemoryRouter>
        <Toaster position="bottom-right" />
      </QueryClientProvider>
    </ThemeProvider>,
  )
}

const ALL_ITEMS = SETTINGS_SECTIONS.flatMap((section) => section.items)

beforeEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('reachability', () => {
  it('lists every settings panel exactly once, grouped into the right sections', () => {
    stubFetch()
    renderSettings()

    for (const section of SETTINGS_SECTIONS) {
      for (const item of section.items) {
        expect(screen.getAllByRole('button', { name: item.label })).toHaveLength(1)
      }
    }
    expect(ALL_ITEMS).toHaveLength(14)
  })

  it('opens the default Business profile panel and marks it current', async () => {
    stubFetch()
    renderSettings()

    const business = await screen.findByRole('button', { name: 'Business profile' })
    expect(business).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('button', { name: 'Staff' })).not.toHaveAttribute('aria-current')
  })

  it('selecting a sidebar item moves aria-current to it', async () => {
    stubFetch()
    const user = userEvent.setup()
    renderSettings()

    await user.click(await screen.findByRole('button', { name: 'Staff' }))

    expect(screen.getByRole('button', { name: 'Staff' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('button', { name: 'Business profile' })).not.toHaveAttribute(
      'aria-current',
    )
  })
})

describe('?tab= deep links', () => {
  it('opens the linked panel and expands its section, even one collapsed by a saved preference', async () => {
    // "Clients & records" saved closed from a previous visit — the deep link must override it.
    localStorage.setItem(
      'linsuite.settings.sections-open',
      JSON.stringify({ clients: false }),
    )
    stubFetch()
    renderSettings('/settings?tab=notes')

    const notes = await screen.findByRole('button', { name: 'Note templates' })
    expect(notes).toHaveAttribute('aria-current', 'page')
    // The section's other item is visible too — proof the section is open, not just that
    // the active button itself survived.
    expect(screen.getByRole('button', { name: 'Forms' })).toBeInTheDocument()
  })

  it('falls back to Business profile for an unrecognised tab', async () => {
    stubFetch()
    renderSettings('/settings?tab=nonsense')

    expect(await screen.findByRole('button', { name: 'Business profile' })).toHaveAttribute(
      'aria-current',
      'page',
    )
  })
})

describe('rail collapse', () => {
  it('collapses to icon-only buttons that keep their accessible names, and remembers it', async () => {
    stubFetch()
    const user = userEvent.setup()
    const { unmount } = renderSettings()

    await user.click(screen.getByRole('button', { name: 'Collapse settings sidebar' }))
    // Collapsed or not, the item is still reachable by the same accessible name (a tooltip
    // now carries the label instead of visible text).
    expect(screen.getByRole('button', { name: 'Staff' })).toBeInTheDocument()
    expect(localStorage.getItem('linsuite.settings.rail-collapsed')).toBe('1')

    unmount()
    renderSettings()
    expect(await screen.findByRole('button', { name: 'Expand settings sidebar' })).toBeInTheDocument()
  })
})

describe('section collapse', () => {
  it('collapses a section, hiding its items, and remembers it across a remount', async () => {
    stubFetch()
    const user = userEvent.setup()
    const { unmount } = renderSettings()

    await user.click(await screen.findByRole('button', { name: 'Catalog' }))
    expect(screen.queryByRole('button', { name: 'Services' })).not.toBeInTheDocument()

    unmount()
    renderSettings()
    // Still on Business profile, so Catalog (not the active section) stays collapsed.
    expect(await screen.findByRole('button', { name: 'Business profile' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Services' })).not.toBeInTheDocument()
  })

  it('keeps the active item\'s section open even if it was saved collapsed', async () => {
    localStorage.setItem('linsuite.settings.sections-open', JSON.stringify({ business: false }))
    stubFetch()
    renderSettings('/settings?tab=branding')

    expect(await screen.findByRole('button', { name: 'Branding' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Business profile' })).toBeInTheDocument()
  })
})
