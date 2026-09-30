import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

afterEach(() => vi.unstubAllGlobals())

// Settings stays on screen when Admin Mode *lapses* (a half-typed draft survives a trip to
// another tab), but choosing Staff Mode on purpose leaves it: Staff Mode shows no admin screen.
test('choosing Staff Mode on an admin-only page leaves it for Home', async () => {
  const user = userEvent.setup()
  stubApi({ signedIn: true, adminWindowMs: 600_000 })

  renderApp('/settings')
  expect(await screen.findByRole('button', { name: 'Business profile' })).toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: /Switch mode/ }))
  await user.click(
    await screen.findByRole('menuitem', { name: /Staff Mode/ }, { timeout: 5_000 }),
  )

  await waitFor(
    () => expect(screen.queryByRole('button', { name: 'Business profile' })).not.toBeInTheDocument(),
    { timeout: 5_000 },
  )
  expect(screen.queryByText('This area needs Admin Mode')).not.toBeInTheDocument()
}, 15_000)
