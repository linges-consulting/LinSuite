import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { BOOKED, fakeServer, onTheFifteenth, renderSchedule } from './schedule-lifecycle-harness'

// Its own file — see `schedule-lifecycle-complete.test.tsx`'s note.

beforeEach(() => vi.unstubAllGlobals())
afterEach(() => vi.useRealTimers())

test('the card menu marks a no-show', async () => {
  onTheFifteenth()
  const calls = fakeServer([BOOKED])
  const user = userEvent.setup()
  renderSchedule()

  await user.click(await screen.findByRole('button', { name: 'Actions for Priya Nair' }))
  await user.click(await screen.findByRole('menuitem', { name: 'No-show' }))

  await waitFor(() =>
    expect(calls.some((c) => c.url === `/api/appointments/${BOOKED.id}/no-show` && c.method === 'POST')).toBe(true),
  )
  const bo = await screen.findByRole('region', { name: 'Bo Chen' })
  expect(within(bo).getByRole('img', { name: 'No-show' })).toBeInTheDocument()
})
