import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { GROUP_ID, LINK_A, LINK_B, fakeServer, onTheFifteenth, renderSchedule } from './schedule-lifecycle-harness'

// Its own file — see `schedule-lifecycle-complete.test.tsx`'s note.

beforeEach(() => vi.unstubAllGlobals())
afterEach(() => vi.useRealTimers())

test('cancelling a linked appointment offers "Cancel visit", which cancels every member', async () => {
  onTheFifteenth()
  const calls = fakeServer([LINK_A, LINK_B])
  const user = userEvent.setup()
  renderSchedule()

  // Both links are Priya's, so the menu is opened by id — one card per staff column.
  await user.click(await screen.findByTestId(`menu-${LINK_A.id}`))
  expect(await screen.findByRole('menuitem', { name: 'Cancel visit' })).toBeInTheDocument()
  await user.click(screen.getByRole('menuitem', { name: 'Cancel visit' }))
  const confirm = await screen.findByRole('dialog', { name: 'Cancel this visit?' })
  await user.click(within(confirm).getByRole('button', { name: 'Cancel it' }))

  await waitFor(() =>
    expect(calls.some((c) => c.url === `/api/appointments/group/${GROUP_ID}/cancel` && c.method === 'POST')).toBe(
      true,
    ),
  )
  await waitFor(() => expect(screen.queryByTestId(`event-${LINK_A.id}`)).not.toBeInTheDocument())
})
