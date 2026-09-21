import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { BOOKED, fakeServer, onTheFifteenth, renderSchedule } from './schedule-lifecycle-harness'

// Its own file on purpose, like `tests/user-menu.test.tsx`: once `schedule.test.tsx`'s many
// drag tests (or a second `SchedulePage` render in this same module) have run, a Radix
// `DropdownMenu` opened on `pointerdown` stops responding to a fresh `userEvent.setup()`'s
// pointerdown. One render, one file, sidesteps it.

beforeEach(() => vi.unstubAllGlobals())
afterEach(() => vi.useRealTimers())

test('the card menu completes an appointment', async () => {
  onTheFifteenth()
  const calls = fakeServer([BOOKED])
  const user = userEvent.setup()
  renderSchedule()

  await user.click(await screen.findByRole('button', { name: 'Actions for Priya Nair' }))
  await user.click(await screen.findByRole('menuitem', { name: 'Complete' }))

  await waitFor(() =>
    expect(calls.some((c) => c.url === `/api/appointments/${BOOKED.id}/complete` && c.method === 'POST')).toBe(true),
  )
  expect(await screen.findByText('Completed Priya Nair')).toBeInTheDocument()
  const bo = screen.getByRole('region', { name: 'Bo Chen' })
  expect(within(bo).getByRole('img', { name: 'Completed' })).toBeInTheDocument()
})
