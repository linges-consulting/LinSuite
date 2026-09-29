import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * Settings → Packages (#97/#95): a new tab beside Products for package definitions
 * (`billing.manage`, Admin Mode) — an empty slot for the ticket that builds it.
 */

afterEach(() => vi.unstubAllGlobals())

test('Settings offers a Packages tab, and it opens empty', async () => {
  const user = userEvent.setup()
  stubApi({ signedIn: true, dualRole: true, adminWindowMs: 900_000 })
  renderApp('/settings')

  await user.click(await screen.findByRole('tab', { name: 'Packages' }))

  expect(await screen.findByText('Packages are not built yet')).toBeInTheDocument()
})
