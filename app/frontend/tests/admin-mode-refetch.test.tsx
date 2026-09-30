import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import App from '@/App'
import { NO_MFA } from './harness'
import { Toaster } from '@/components/ui/sonner'
import { createQueryClient } from '@/lib/query-client'
import { ThemeProvider } from '@/lib/theme'

// Its own file, and one `userEvent.setup()` for the whole of it: a Radix menu opens on
// `pointerdown` and user-event's pointer state is per-document, so a second setup in the
// same file leaves the trigger unable to open. Same reason as tests/mode-switch.test.tsx.

const user = userEvent.setup()

afterEach(() => vi.unstubAllGlobals())

/**
 * Found in a browser rather than in a test: after entering Admin Mode, the Settings panel
 * went on showing "Switch to Admin Mode to do this", because the refusal was cached and
 * nothing had asked the server again. Changing mode changes what the server will answer, so
 * everything fetched under the old mode is stale by definition.
 *
 * #114 (spec #113 Staff Mode section) closed the specific hole this test found a different
 * way: `/settings` is now a whole Admin-only route (`RequireAdminMode` in `App.tsx`), so
 * `SettingsPage` never mounts — and never fetches anything to cache a refusal from — while
 * the session is in Staff Mode. What is left to prove is the same user-facing promise the
 * title always meant: entering Admin Mode brings the panel up on its own, no reload and no
 * second click, whichever mechanism gets it there.
 */
test('entering Admin Mode brings Settings up on its own, with no reload', async () => {
  let elevated = false
  const session = () => ({
    id: 'u1',
    email: 'owner@cedar.example',
    role: 'Administrator',
    capabilities: ['admin', 'roles.manage'],
    mode: elevated ? 'admin' : 'staff',
    can_switch_modes: true,
    // A live grant while still being served in Staff Mode: the "switched back, window still
    // open" case, where returning to Admin Mode costs no password (PRD §1). That keeps this
    // test about the refetch rather than about the re-authentication dialog.
    admin_grant_expires_at: new Date(Date.now() + 900_000).toISOString(),
    admin_hard_limit_at: new Date(Date.now() + 1_800_000).toISOString(),
    must_change_password: false,
    mfa: NO_MFA,
  })

  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url === '/api/setup/status') return Response.json({ required: false })
      if (url === '/api/auth/me') return Response.json(session())
      if (url === '/api/auth/mode') {
        // A live grant already exists in this fake, so the switch costs no password.
        elevated = true
        return Response.json(session())
      }
      if (url.startsWith('/api/admin/')) {
        if (!elevated) {
          return Response.json(
            { detail: 'Switch to Admin Mode to do this.', code: 'admin_mode_required' },
            { status: 403 },
          )
        }
        if (url === '/api/admin/capabilities') return Response.json({ capabilities: [] })
        if (url === '/api/admin/business/provinces') return Response.json([])
        return Response.json({ roles: [], users: [] })
      }
      return Response.json({ status: 'ok', database: 'ok' })
    }),
  )

  render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter initialEntries={['/settings']}>
          <App />
        </MemoryRouter>
        <Toaster position="bottom-right" />
      </QueryClientProvider>
    </ThemeProvider>,
  )

  // Refused first: the window is not open, so the route guard is what shows — never
  // `SettingsPage` itself, and so never a fetch to cache a refusal from.
  expect(await screen.findByText('This area needs Admin Mode', {}, { timeout: 5_000 })).toBeInTheDocument()

  // The guard page's own switcher plus the shell header's — either gets there, so the first
  // one found is as good as any.
  const [switcher] = await screen.findAllByRole('button', { name: /Switch mode/ })
  await user.click(switcher)
  await user.click(await screen.findByRole('menuitem', { name: /Admin Mode/ }))

  // The panel returns on its own — no reload, no second click.
  await user.click(await screen.findByRole('tab', { name: 'Roles' }))
  await waitFor(
    () => expect(screen.getByRole('button', { name: 'New role' })).toBeInTheDocument(),
    { timeout: 5_000 },
  )
  // Generous: the client retries a refused query once before reporting it, so the first
  // assertion alone spends a backoff waiting for a refusal that is meant to arrive.
}, 20_000)
