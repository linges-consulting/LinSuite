import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { BOOKED, fakeServer, onTheFifteenth, renderSchedule } from './schedule-lifecycle-harness'

// One render per file — see `schedule-lifecycle-complete.test.tsx` for why.

beforeEach(() => vi.unstubAllGlobals())
afterEach(() => vi.useRealTimers())

const PACK = {
  package_purchase_id: 'pp1',
  name: '10-Session Massage Pack',
  purchased_at: '2026-06-01T14:00:00Z',
  expires_at: null,
  credits_total: 10,
  credits_remaining: 7,
  value_cents: 9600,
}

test('completing a client with a package asks which pays and sends the chosen purchase', async () => {
  onTheFifteenth()
  const calls = fakeServer([BOOKED], [PACK])
  const user = userEvent.setup()
  renderSchedule()

  await user.click(await screen.findByRole('button', { name: 'Actions for Priya Nair' }))
  await user.click(await screen.findByRole('menuitem', { name: 'Complete' }))

  const pack = await screen.findByRole('radio', { name: '10-Session Massage Pack' })
  expect(pack).toBeChecked()
  expect(screen.getByText(/7 of 10 left · this session \$96\.00/)).toBeInTheDocument()
  expect(calls.some((c) => c.url.endsWith('/complete'))).toBe(false)

  await user.click(screen.getByRole('button', { name: 'Complete' }))

  await waitFor(() => {
    const post = calls.find((c) => c.url === `/api/appointments/${BOOKED.id}/complete`)
    expect(post?.body).toEqual({ package_purchase_id: 'pp1' })
  })
  expect(await screen.findByText('Completed Priya Nair')).toBeInTheDocument()
})
