import { screen, waitFor } from '@testing-library/react'
import { renderApp, stubApi } from './harness'

/**
 * #114 (spec #113 Staff Mode section): the whole-app version of `nav.test.tsx` and
 * `capability-gate.test.tsx` — an account holding every administrative capability there is,
 * walked through every top-level, statically-routed screen while in Staff Mode. Three things
 * must hold everywhere: no Admin-only nav entry, no "Switch to Admin Mode" placeholder
 * outside the calendar's own out-of-availability explanation, and the two whole-route
 * Admin-only screens (Settings, Reports) render the needs-Admin-Mode page rather than any
 * part of themselves.
 *
 * Dynamic detail routes (`/clients/:id`, `/bills/:id`, `/bills/invoices/:id`) already have
 * their own Staff Mode coverage — `clients.test.tsx`'s access-history tests and
 * `bills.test.tsx`'s override-authority tests — so this sweep is the navigable, top-level
 * surface every account reaches from the sidebar or a typed URL.
 */

afterEach(() => vi.unstubAllGlobals())

const ALL_CAPABILITIES = [
  // Administrative — requires Admin Mode (`auth/capabilities.py`'s `requires_admin_mode`).
  'admin',
  'roles.manage',
  'users.manage',
  'catalog.manage',
  'audit.view',
  'forms.manage',
  'notes.manage',
  'customers.erase',
  'billing.manage',
  'commission.view',
  'inventory.receive',
  'inventory.adjust',
  // Front-desk work — never Admin Mode.
  'schedule.view',
  'schedule.manage',
  'schedule.override_availability',
  'customers.view',
  'customers.manage',
  'forms.issue',
  'forms.view',
  'notes.view',
  'notes.write',
  'queue.manage',
  'billing.view',
]

/** Every capability there is, in Staff Mode: the account most likely to reveal a control that
 *  should have been hidden but was not. */
const STAFF_ACCOUNT = {
  id: 'u1',
  email: 'owner@cedar.example',
  role: 'Administrator',
  capabilities: ALL_CAPABILITIES,
  mode: 'staff' as const,
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
}

/** Minimal, valid-shaped answers for the handful of endpoints the top-level screens read
 *  unconditionally on mount — empty lists throughout, since the point of this sweep is what
 *  chrome renders around the data, not the data itself. */
function sweepStub(mode: 'staff' | 'admin' = 'staff') {
  return stubApi({
    signedIn: true,
    adminWindowMs: mode === 'admin' ? 15 * 60_000 : 0,
    respond: (url) => {
      if (url === '/api/auth/me') return Response.json({ ...STAFF_ACCOUNT, mode })
      if (url === '/api/staff') return Response.json({ staff: [] })
      if (url === '/api/catalog/services') return Response.json({ services: [] })
      if (url === '/api/catalog/products') return Response.json({ products: [] })
      if (url.startsWith('/api/queue-entries')) return Response.json({ entries: [] })
      if (url === '/api/retail-sales') return Response.json({ retail_sales: [] })
      if (url === '/api/cti/demo-mode') return Response.json({ enabled: false })
      if (url === '/api/bills') return Response.json({ bills: [] })
      if (url.startsWith('/api/forms/compliance')) return Response.json({ clients: [] })
      if (url.startsWith('/api/customers')) return Response.json({ customers: [], total: 0 })
      if (url.startsWith('/api/schedule')) {
        return Response.json({
          timezone: 'America/Toronto',
          granularity_minutes: 15,
          staff: [],
          working_blocks: [],
          time_off: [],
          closures: [],
          appointments: [],
        })
      }
      return undefined
    },
  })
}

/** The literal placeholder phrase every non-calendar "Switch to Admin Mode" text used to
 *  share (#114) — see `lib/calendar/overrides.ts` for the one place it still belongs. */
const ADMIN_PLACEHOLDER = /switch to admin mode/i

/** Waits for whatever this screen loaded on mount to settle, so a placeholder that only
 *  appears once a query resolves has had its chance to before the negative assertion runs. */
async function settle() {
  await screen.findByRole('navigation', { name: 'Primary' })
  await waitFor(() =>
    expect(document.querySelectorAll('[data-slot="skeleton"]')).toHaveLength(0),
  )
}

const STAFF_ROUTES = [
  '/',
  '/schedule',
  '/queue',
  '/clients',
  '/phone-lookup',
  '/sell',
  '/bills',
  '/security',
]

test.each(STAFF_ROUTES)('%s in Staff Mode offers no Admin-only nav entry or placeholder', async (path) => {
  sweepStub()
  renderApp(path)
  await settle()

  const nav = screen.getByRole('navigation', { name: 'Primary' })
  expect(nav).not.toHaveTextContent('Settings')
  expect(nav).not.toHaveTextContent('Reports')
  expect(screen.queryAllByText(ADMIN_PLACEHOLDER)).toHaveLength(0)
})

test.each(['/settings', '/reports', '/setup-checklist/business'])(
  '%s in Staff Mode shows the needs-Admin-Mode page instead of its own screen',
  async (path) => {
    sweepStub()
    renderApp(path)

    expect(await screen.findByText('This area needs Admin Mode')).toBeInTheDocument()
    // The mode switcher — the one control the guard page is required to offer.
    expect(screen.getAllByLabelText(/Switch mode/).length).toBeGreaterThan(0)
    expect(screen.queryAllByText(ADMIN_PLACEHOLDER)).toHaveLength(0)
  },
)

test('the same account in Admin Mode is offered Settings and Reports', async () => {
  sweepStub('admin')
  renderApp('/')

  expect(await screen.findByRole('link', { name: 'Settings' })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Reports' })).toBeInTheDocument()
})
