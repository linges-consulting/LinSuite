import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp, stubApi } from './harness'

/**
 * The checklist's focused step pages (#117, spec #113): each step opens its real Settings
 * panel full-page, with Back/Next in the checklist's own fixed order and a link back to it —
 * and the Staff step's "Add me as a practitioner" shortcut.
 */

afterEach(() => vi.unstubAllGlobals())

const ADMIN = { signedIn: true, dualRole: true, adminWindowMs: 15 * 60_000 }

const PALETTE = [
  {
    key: 'blue',
    name: 'Blue',
    hex: '#1d4ed8',
    dark_hex: '#659dff',
    foreground: '#ffffff',
    dark_foreground: '#0f172a',
  },
]

function ownerStaffRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 's1',
    user_id: 'u1',
    email: 'owner@cedar.example',
    role: 'Administrator',
    role_id: 'r-admin',
    locked_until: null,
    mfa_enrolled: true,
    first_name: 'Ada',
    last_name: 'Okonkwo',
    display_name: 'Ada Okonkwo',
    is_practitioner: false,
    designation: null,
    licence_number: null,
    commission_rate_services_bp: 0,
    commission_rate_retail_bp: 0,
    colour: 'blue',
    max_concurrent_appointments: 1,
    active: true,
    sort_order: 0,
    invite_pending: false,
    ...overrides,
  }
}

// --- gating ---------------------------------------------------------------------------------

test('an account without the admin capability sees the Admin Mode notice, not the panel', async () => {
  stubApi({ signedIn: true, dualRole: false })

  renderApp('/setup-checklist/business')

  expect(await screen.findByText('This area needs Admin Mode')).toBeInTheDocument()
  expect(screen.queryByText('Business details')).not.toBeInTheDocument()
})

test('an administrator in Staff Mode sees the same notice', async () => {
  stubApi({ signedIn: true, dualRole: true, adminWindowMs: 0 })

  renderApp('/setup-checklist/business')

  expect(await screen.findByText('This area needs Admin Mode')).toBeInTheDocument()
})

test('an unknown step falls back to the checklist on Home', async () => {
  stubApi(ADMIN)

  renderApp('/setup-checklist/not-a-real-step')

  expect(await screen.findByText('Your workspace is ready')).toBeInTheDocument()
})

// --- Back / Next -------------------------------------------------------------------------------

test('Next walks the fixed step order, and Back returns the same way', async () => {
  const user = userEvent.setup()
  stubApi({
    ...ADMIN,
    respond: (url) => {
      if (url === '/api/admin/staff') return Response.json({ staff: [ownerStaffRow()] })
      if (url === '/api/admin/staff/palette') return Response.json({ colours: PALETTE })
      if (url === '/api/admin/roles') return Response.json({ roles: [] })
      return undefined
    },
  })

  renderApp('/setup-checklist/business')

  expect(await screen.findByRole('heading', { name: 'Business details' })).toBeInTheDocument()
  expect(screen.getByText('Step 1 of 8')).toBeInTheDocument()

  await user.click(screen.getByRole('link', { name: 'Next' }))
  expect(await screen.findByRole('heading', { name: 'Opening hours' })).toBeInTheDocument()
  expect(screen.getByText('Step 2 of 8')).toBeInTheDocument()

  await user.click(screen.getByRole('link', { name: 'Back' }))
  expect(await screen.findByRole('heading', { name: 'Business details' })).toBeInTheDocument()
})

test('spaces come right before services, because a service can require one', async () => {
  const user = userEvent.setup()
  stubApi({
    ...ADMIN,
    respond: (url) => {
      if (url.startsWith('/api/admin/resources')) return Response.json({ resources: [] })
      return undefined
    },
  })

  renderApp('/setup-checklist/spaces')

  expect(await screen.findByRole('heading', { name: 'Spaces' })).toBeInTheDocument()
  expect(screen.getByText('Step 4 of 8')).toBeInTheDocument()

  await user.click(screen.getByRole('link', { name: 'Next' }))
  expect(await screen.findByRole('heading', { name: 'Services' })).toBeInTheDocument()

  await user.click(screen.getByRole('link', { name: 'Back' }))
  await user.click(await screen.findByRole('link', { name: 'Back' }))
  expect(await screen.findByRole('heading', { name: 'Tax' })).toBeInTheDocument()
})

test('the link back to the checklist goes Home', async () => {
  const user = userEvent.setup()
  stubApi(ADMIN)

  renderApp('/setup-checklist/business')
  await screen.findByRole('heading', { name: 'Business details' })

  await user.click(screen.getByRole('link', { name: 'Back to checklist' }))

  expect(await screen.findByText('Your workspace is ready')).toBeInTheDocument()
})

// --- Add me as a practitioner ----------------------------------------------------------------

test('the staff step offers to add the signed-in administrator as a practitioner', async () => {
  const user = userEvent.setup()
  let staffRow = ownerStaffRow()
  const { calls } = stubApi({
    ...ADMIN,
    respond: (url, body) => {
      if (url === '/api/admin/staff') return Response.json({ staff: [staffRow] })
      if (url === '/api/admin/staff/palette') return Response.json({ colours: PALETTE })
      if (url === '/api/admin/roles') return Response.json({ roles: [] })
      if (url === '/api/admin/staff/s1') {
        staffRow = { ...staffRow, ...body }
        return Response.json(staffRow)
      }
      return undefined
    },
  })

  renderApp('/setup-checklist/staff')

  const prompt = await screen.findByRole('button', { name: 'Add me as a practitioner' })
  await user.click(prompt)

  // The dialog opens on the existing row, pre-checked.
  const dialog = await screen.findByRole('dialog')
  expect(dialog).toHaveTextContent('Edit Ada Okonkwo')
  expect(screen.getByLabelText('Practitioner')).toBeChecked()

  // The DB-enforced credentials still have to be filled in — this is a pre-fill, not a
  // zero-input action — and Save stays disabled until they are.
  expect(screen.getByRole('button', { name: 'Save staff member' })).toBeDisabled()
  await user.type(screen.getByLabelText('Designation'), 'RMT')
  await user.type(screen.getByLabelText('Licence number'), '#98765')
  await user.click(screen.getByRole('button', { name: 'Save staff member' }))

  await waitFor(() =>
    expect(
      calls.some(
        (c) =>
          c.url === '/api/admin/staff/s1' &&
          c.method === 'PATCH' &&
          (c.body as any).is_practitioner === true &&
          (c.body as any).designation === 'RMT' &&
          (c.body as any).licence_number === '#98765',
      ),
    ).toBe(true),
  )
  // The banner is gone once the row is a practitioner.
  await waitFor(() =>
    expect(screen.queryByRole('button', { name: 'Add me as a practitioner' })).not.toBeInTheDocument(),
  )
})

test('the shortcut is not offered once the signed-in administrator is already a practitioner', async () => {
  stubApi({
    ...ADMIN,
    respond: (url) => {
      if (url === '/api/admin/staff') {
        return Response.json({
          staff: [ownerStaffRow({ is_practitioner: true, designation: 'RMT', licence_number: '#1' })],
        })
      }
      if (url === '/api/admin/staff/palette') return Response.json({ colours: PALETTE })
      if (url === '/api/admin/roles') return Response.json({ roles: [] })
      return undefined
    },
  })

  renderApp('/setup-checklist/staff')

  await screen.findByRole('button', { name: 'Add staff member' })
  expect(screen.queryByRole('button', { name: 'Add me as a practitioner' })).not.toBeInTheDocument()
})
