import { screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { LINK_A, LINK_B, TEN, fakeServer, onTheFifteenth, renderSchedule } from './schedule-lifecycle-harness'

beforeEach(() => vi.unstubAllGlobals())
afterEach(() => vi.useRealTimers())

test('a completed member of a group has no card menu, and stays completed', async () => {
  onTheFifteenth()
  fakeServer([LINK_A, { ...LINK_B, status: 'completed', completed_at: TEN }])
  renderSchedule()

  // LINK_B is already completed: its own menu never renders, terminal in M1 (no reopen).
  const ana = await screen.findByRole('region', { name: 'Ana Rossi' })
  expect(within(ana).queryByTestId(`menu-${LINK_B.id}`)).not.toBeInTheDocument()
  expect(within(ana).getByRole('img', { name: 'Completed' })).toBeInTheDocument()
})
