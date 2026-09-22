import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi, type Call } from './harness'

afterEach(() => vi.unstubAllGlobals())

/**
 * The Clients list and the profile behind a row.
 *
 * What is worth pinning: the list is the server's page (not a client-side filter), typing
 * narrows it through `q` and nothing else, an empty answer is the standard `EmptyState`, a
 * row opens `/clients/:id`, and the profile renders the client and their visits from the one
 * response — one request, because on the server that request is one access-log row.
 */

const PRIYA = {
  id: 'c1',
  first_name: 'Priya',
  last_name: 'Nair',
  email: 'priya@example.com',
  phone: '4165550199',
  created_at: '2026-01-05T15:00:00Z',
  classification: 'vip' as const,
}
const SAM = {
  id: 'c2',
  first_name: 'Sam',
  last_name: 'Okonkwo',
  email: null,
  phone: null,
  created_at: '2026-02-01T15:00:00Z',
  classification: 'new' as const,
}

const PRIYA_DETAIL = {
  ...PRIYA,
  date_of_birth: null,
  emergency_contact_name: null,
  emergency_contact_phone: null,
  emergency_contact_relationship: null,
  secondary_contact_name: null,
  secondary_contact_phone: null,
  secondary_contact_email: null,
  notes: null,
  updated_at: '2026-01-05T15:00:00Z',
}

const PROFILE = {
  customer: PRIYA_DETAIL,
  timezone: 'America/Toronto',
  appointments: [
    {
      id: 'a1',
      starts_at: '2026-10-05T14:00:00Z',
      ends_at: '2026-10-05T15:00:00Z',
      status: 'confirmed',
      booking_group_id: null,
      service: { id: 'v1', name: 'Swedish Massage' },
      staff: { id: 's1', display_name: 'Ana Rossi', colour: 'blue' },
    },
    {
      id: 'a2',
      starts_at: '2026-03-02T15:00:00Z',
      ends_at: '2026-03-02T16:00:00Z',
      status: 'no_show',
      booking_group_id: null,
      service: { id: 'v1', name: 'Swedish Massage' },
      staff: { id: 's2', display_name: 'Bo Chen', colour: 'teal' },
    },
  ],
}

/** An account holding `customers.manage`, so the edit dialog is reachable. Mirrors the shape
 *  `/api/auth/me` actually sends — the app shell reads `mode` and `mfa` too. */
const ME_WITH_MANAGE = {
  id: 'u1',
  email: 'owner@cedar.example',
  role: 'Front desk',
  capabilities: ['customers.view', 'customers.manage', 'schedule.view'],
  mode: 'staff',
  can_switch_modes: false,
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

function fake({ canEdit = false }: { canEdit?: boolean } = {}) {
  let profile = { ...PROFILE }
  return stubApi({
    signedIn: true,
    dualRole: false,
    respond: (url: string, body: unknown) => {
      const parsed = new URL(url, 'http://test')
      if (canEdit && url === '/api/auth/me') return Response.json(ME_WITH_MANAGE)
      if (parsed.pathname === '/api/customers') {
        const q = (parsed.searchParams.get('q') ?? '').toLowerCase()
        const digits = q.replace(/\D/g, '')
        const customers = [SAM, PRIYA].filter(
          (c) =>
            c.last_name.toLowerCase().startsWith(q) ||
            (digits.length > 0 && (c.phone ?? '').startsWith(digits)),
        )
        return Response.json({ customers, total: customers.length })
      }
      if (parsed.pathname === '/api/customers/c1') {
        // A PATCH carries a body; a GET (the profile open) does not — the fake tells the
        // two apart the same way it tells every other pair of verbs on one path apart.
        if (body !== undefined) {
          if ((body as { email?: string }).email === 'taken@example.com') {
            return Response.json(
              { detail: 'A customer with that email already exists.' },
              { status: 409 },
            )
          }
          profile = { ...profile, customer: { ...profile.customer, ...(body as object) } }
          return Response.json(profile.customer)
        }
        return Response.json(profile)
      }
      return undefined
    },
  })
}

const listCalls = (calls: Call[]) => calls.filter((c) => c.url.startsWith('/api/customers?'))

test('the list renders the server page with the phone formatted for a person', async () => {
  fake()

  renderApp('/clients')

  const table = await screen.findByRole('table')
  expect(within(table).getByText('Priya Nair')).toBeInTheDocument()
  expect(within(table).getByText('(416) 555-0199')).toBeInTheDocument()
  expect(within(table).getByText('priya@example.com')).toBeInTheDocument()
  expect(within(table).getByText('Sam Okonkwo')).toBeInTheDocument()
  expect(screen.getByText('2 clients')).toBeInTheDocument()
  // Classification on every row — VIP for Priya, New for Sam, never derived client-side.
  expect(within(table).getByText('VIP')).toBeInTheDocument()
  expect(within(table).getByText('New')).toBeInTheDocument()
})

test('typing in the search box narrows the list through q', async () => {
  const { calls } = fake()
  const user = userEvent.setup()
  renderApp('/clients')
  await screen.findByText('Sam Okonkwo')

  await user.type(screen.getByRole('searchbox', { name: 'Search clients' }), 'nai')

  await waitFor(() => expect(screen.queryByText('Sam Okonkwo')).not.toBeInTheDocument())
  expect(screen.getByText('Priya Nair')).toBeInTheDocument()
  const last = listCalls(calls).at(-1)!
  expect(new URL(last.url, 'http://test').searchParams.get('q')).toBe('nai')
})

test('a formatted phone number finds the client too', async () => {
  fake()
  const user = userEvent.setup()
  renderApp('/clients')
  await screen.findByText('Sam Okonkwo')

  await user.type(screen.getByRole('searchbox', { name: 'Search clients' }), '(416) 555')

  await waitFor(() => expect(screen.queryByText('Sam Okonkwo')).not.toBeInTheDocument())
  expect(screen.getByText('Priya Nair')).toBeInTheDocument()
})

test('no match is the standard empty state', async () => {
  fake()
  const user = userEvent.setup()
  renderApp('/clients')
  await screen.findByText('Sam Okonkwo')

  await user.type(screen.getByRole('searchbox', { name: 'Search clients' }), 'zzz')

  expect(await screen.findByText('No clients match')).toBeInTheDocument()
  expect(screen.queryByRole('table')).not.toBeInTheDocument()
})

test('a row opens the profile, which loads client and visits in one request', async () => {
  const { calls } = fake()
  const user = userEvent.setup()
  renderApp('/clients')
  await screen.findByText('Priya Nair')

  // The cell, not the link: the whole row is the target.
  await user.click(screen.getByText('(416) 555-0199'))

  expect(await screen.findByRole('heading', { name: 'Priya Nair' })).toBeInTheDocument()
  expect(screen.getByText('priya@example.com')).toBeInTheDocument()
  const visits = screen.getByRole('table', { name: 'Appointments' })
  const rows = within(visits).getAllByRole('row').slice(1)
  expect(rows).toHaveLength(2)
  expect(within(rows[0]).getByText('Swedish Massage')).toBeInTheDocument()
  expect(within(rows[0]).getByText('Ana Rossi')).toBeInTheDocument()
  expect(within(rows[0]).getByText('Confirmed')).toBeInTheDocument()
  // 14:00Z on 5 October is 10:00 in Toronto: the business's clock, not UTC.
  expect(within(rows[0]).getByText(/10:00/)).toBeInTheDocument()
  expect(within(rows[1]).getByText('Bo Chen')).toBeInTheDocument()
  expect(within(rows[1]).getByText('No show')).toBeInTheDocument()
  expect(calls.filter((c) => c.url === '/api/customers/c1')).toHaveLength(1)
})

test('a profile with no visits says so', async () => {
  stubApi({
    signedIn: true,
    dualRole: false,
    respond: (url: string) =>
      url === '/api/customers/c1'
        ? Response.json({ ...PROFILE, appointments: [] })
        : undefined,
  })

  renderApp('/clients/c1')

  expect(await screen.findByRole('heading', { name: 'Priya Nair' })).toBeInTheDocument()
  expect(screen.getByText('No appointments yet')).toBeInTheDocument()
})

/**
 * The "Edit details" dialog. What is worth pinning: only the fields somebody actually
 * touched are on the wire (never the whole draft, and never a value nobody changed), and a
 * refusal from the server lands under the field it is about rather than as a bare toast.
 */

test('a customers.view-only session sees no edit control', async () => {
  fake({ canEdit: false })
  renderApp('/clients/c1')
  await screen.findByRole('heading', { name: 'Priya Nair' })

  expect(screen.queryByRole('button', { name: 'Edit details' })).not.toBeInTheDocument()
})

test('the edit dialog submits only the field that changed', async () => {
  const { calls } = fake({ canEdit: true })
  const user = userEvent.setup()
  renderApp('/clients/c1')
  await user.click(await screen.findByRole('button', { name: 'Edit details' }))

  await user.type(screen.getByLabelText('Notes'), 'Prefers the corner chair.')
  await user.click(screen.getByRole('button', { name: 'Save changes' }))

  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  const patches = calls.filter((c) => c.url === '/api/customers/c1' && c.method === 'PATCH')
  expect(patches).toHaveLength(1)
  expect(patches[0].body).toEqual({ notes: 'Prefers the corner chair.' })
})

test('a duplicate email refusal lands under the email field', async () => {
  fake({ canEdit: true })
  const user = userEvent.setup()
  renderApp('/clients/c1')
  await user.click(await screen.findByRole('button', { name: 'Edit details' }))

  // `getByLabelText('Email')` would be ambiguous — the secondary contact has one too.
  // `fireEvent.change` rather than `user.type`: the field starts with Priya's own address,
  // and typing would append onto it instead of replacing it.
  fireEvent.change(document.getElementById('client-email')!, {
    target: { value: 'taken@example.com' },
  })
  await user.click(screen.getByRole('button', { name: 'Save changes' }))

  const emailField = (await screen.findByText('A customer with that email already exists.'))
    .closest('div')!
  expect(within(emailField).getByText('Email')).toBeInTheDocument()
  expect(screen.getByRole('dialog')).toBeInTheDocument()
})

test('a date of birth in the future is refused before the round trip', async () => {
  fake({ canEdit: true })
  const user = userEvent.setup()
  renderApp('/clients/c1')
  await user.click(await screen.findByRole('button', { name: 'Edit details' }))

  const tomorrow = new Date(Date.now() + 86_400_000).toISOString().slice(0, 10)
  const dob = screen.getByLabelText('Date of birth')
  fireEvent.change(dob, { target: { value: tomorrow } })
  fireEvent.blur(dob)

  expect(screen.getByText('Date of birth cannot be in the future.')).toBeInTheDocument()
})
