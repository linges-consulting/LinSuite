import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { BOOKED, fakeServer, onTheFifteenth, renderSchedule } from './schedule-lifecycle-harness'

// Its own file — see `schedule-lifecycle-complete.test.tsx`'s note.

beforeEach(() => vi.unstubAllGlobals())
afterEach(() => vi.useRealTimers())

test('the card menu cancels one appointment with a reason, through a confirm dialog', async () => {
  onTheFifteenth()
  const calls = fakeServer([BOOKED])
  const user = userEvent.setup()
  renderSchedule()

  await user.click(await screen.findByRole('button', { name: 'Actions for Priya Nair' }))
  await user.click(await screen.findByRole('menuitem', { name: 'Cancel' }))
  const confirm = await screen.findByRole('dialog', { name: 'Cancel this appointment?' })
  await user.type(within(confirm).getByLabelText('Reason (optional)'), 'Client called it off')
  await user.click(within(confirm).getByRole('button', { name: 'Cancel it' }))

  await waitFor(() =>
    expect(calls.some((c) => c.url === `/api/appointments/${BOOKED.id}/cancel` && c.method === 'POST')).toBe(true),
  )
  const cancel = calls.find((c) => c.url === `/api/appointments/${BOOKED.id}/cancel`)!
  expect(cancel.body).toEqual({ reason: 'Client called it off' })
  // The default view leaves a cancelled appointment out, so the card disappears.
  await waitFor(() => expect(screen.queryByTestId(`event-${BOOKED.id}`)).not.toBeInTheDocument())
})
