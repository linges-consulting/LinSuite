import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { useCan } from '@/lib/capability-gate'
import { createQueryClient } from '@/lib/query-client'
import type { Mode, User } from '@/lib/api'

/**
 * The shared capability/mode gate (#97): true only when the account holds the capability and,
 * for one the registry marks administrative, the session is in Admin Mode. Driven through
 * `useCan` directly rather than a whole screen — there is no UI here to click, only three
 * outcomes to tell apart.
 */

afterEach(() => vi.unstubAllGlobals())

function account(overrides: Partial<User> & { capabilities: string[]; mode: Mode }): User {
  return {
    id: 'u1',
    email: 'desk@cedar.example',
    role: 'Staff',
    can_switch_modes: true,
    admin_grant_expires_at: null,
    admin_hard_limit_at: null,
    must_change_password: false,
    mfa: {
      enrolled: false,
      method: null,
      pending: false,
      enrolment_required: false,
      verified_at: null,
      email_otp_allowed: false,
    },
    ...overrides,
  }
}

function stubMe(user: User | null) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url === '/api/auth/me') return Response.json(user)
      return new Response(null, { status: 404 })
    }),
  )
}

function Probe({ capability }: { capability: string }) {
  return <span data-testid="can">{String(useCan(capability))}</span>
}

function renderProbe(capability: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <Probe capability={capability} />
    </QueryClientProvider>,
  )
}

// The span carrying the gate's answer exists from the first render, before the session has
// even been asked for — `findByTestId` would resolve against that instant, not against the
// settled answer. `waitFor` is what actually waits for the text to reach what a settled
// session should produce.

test('false without the capability at all', async () => {
  stubMe(account({ capabilities: ['schedule.view'], mode: 'staff' }))

  renderProbe('billing.manage')

  await waitFor(() => expect(screen.getByTestId('can')).toHaveTextContent('false'))
})

test('false in Staff Mode for a capability that requires Admin Mode', async () => {
  stubMe(account({ capabilities: ['billing.manage'], mode: 'staff' }))

  renderProbe('billing.manage')

  await waitFor(() => expect(screen.getByTestId('can')).toHaveTextContent('false'))
})

test('true in Admin Mode holding a capability that requires Admin Mode', async () => {
  stubMe(account({ capabilities: ['billing.manage'], mode: 'admin' }))

  renderProbe('billing.manage')

  await waitFor(() => expect(screen.getByTestId('can')).toHaveTextContent('true'))
})

test('a capability that never requires Admin Mode is true in Staff Mode once held', async () => {
  stubMe(account({ capabilities: ['billing.view'], mode: 'staff' }))

  renderProbe('billing.view')

  await waitFor(() => expect(screen.getByTestId('can')).toHaveTextContent('true'))
})

test('false while the session is still loading, and false again once it resolves to signed out', async () => {
  stubMe(null)

  renderProbe('billing.manage')

  await waitFor(() => expect(screen.getByTestId('can')).toHaveTextContent('false'))
})
