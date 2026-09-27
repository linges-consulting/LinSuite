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
  retention: { status: 'not_held' as const, expires_on: null as string | null },
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
      if (url === '/api/customers' && body && typeof body === 'object' && 'first_name' in body) {
        const draft = body as { first_name: string; last_name: string; email: string | null; phone: string | null }
        if (draft.email === 'taken@example.com') {
          return Response.json(
            { detail: 'A customer with that email already exists.' },
            { status: 409 },
          )
        }
        return Response.json(
          { id: 'c3', ...draft, classification: 'new' },
          { status: 201 },
        )
      }
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
      if (parsed.pathname === '/api/customers/c3') {
        return Response.json({
          customer: {
            id: 'c3',
            first_name: 'New',
            last_name: 'Client',
            email: null,
            phone: null,
            classification: 'new',
            created_at: '2026-09-22T12:00:00Z',
            date_of_birth: null,
            emergency_contact_name: null,
            emergency_contact_phone: null,
            emergency_contact_relationship: null,
            secondary_contact_name: null,
            secondary_contact_phone: null,
            secondary_contact_email: null,
            notes: null,
            updated_at: '2026-09-22T12:00:00Z',
            retention: { status: 'not_held', expires_on: null },
          },
          timezone: 'America/Toronto',
          appointments: [],
        })
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

// --- gap: the list had no way to create a client, only the booking dialog did -----------------

test('without customers.manage there is no "New client" button', async () => {
  fake()
  renderApp('/clients')
  await screen.findByText('Priya Nair')

  expect(screen.queryByRole('button', { name: 'New client' })).not.toBeInTheDocument()
})

test('"New client" opens a dialog that posts to the customers API and opens the new profile', async () => {
  const { calls } = fake({ canEdit: true })
  const user = userEvent.setup()
  renderApp('/clients')
  await screen.findByText('Priya Nair')

  await user.click(screen.getByRole('button', { name: 'New client' }))
  const dialog = await screen.findByRole('dialog', { name: 'New client' })
  // Nothing to send yet: the button stays off rather than posting a blank client.
  expect(within(dialog).getByRole('button', { name: 'Create client' })).toBeDisabled()

  await user.type(within(dialog).getByLabelText('First name'), 'New')
  await user.type(within(dialog).getByLabelText('Last name'), 'Client')
  await user.type(within(dialog).getByLabelText('Phone'), '4165550100')
  await user.click(within(dialog).getByRole('button', { name: 'Create client' }))

  await waitFor(() => {
    const created = calls.find((c) => c.method === 'POST' && c.url === '/api/customers')
    expect(created?.body).toEqual({
      first_name: 'New',
      last_name: 'Client',
      email: null,
      phone: '4165550100',
    })
  })
  expect(await screen.findByRole('heading', { name: 'New Client' })).toBeInTheDocument()
})

test('a duplicate email creating a client lands under the email field, same as editing one does', async () => {
  fake({ canEdit: true })
  const user = userEvent.setup()
  renderApp('/clients')
  await screen.findByText('Priya Nair')

  await user.click(screen.getByRole('button', { name: 'New client' }))
  const dialog = await screen.findByRole('dialog', { name: 'New client' })
  await user.type(within(dialog).getByLabelText('First name'), 'New')
  await user.type(within(dialog).getByLabelText('Last name'), 'Client')
  await user.type(within(dialog).getByLabelText('Email'), 'taken@example.com')
  await user.click(within(dialog).getByRole('button', { name: 'Create client' }))

  expect(
    await within(dialog).findByText('A customer with that email already exists.'),
  ).toBeInTheDocument()
  // Refused, not created: the dialog stays open on the same screen.
  expect(screen.getByRole('dialog', { name: 'New client' })).toBeInTheDocument()
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

/** The "Records" line: when this chart may be destroyed (ADR-0001), in its three states. */
test.each([
  // The browser's own date style, as for the DOB: "14 Mar 2041" in en-CA, "Mar 14, 2041" here.
  [{ status: 'held', expires_on: '2041-03-14' }, /^Held until (14 Mar|Mar 14,) 2041$/, false],
  [{ status: 'not_held', expires_on: null }, 'Not under a retention hold', false],
  // A hold whose date has passed is not a hold: never "Held until" a day already gone.
  [
    { status: 'expired', expires_on: '2025-08-01' },
    /^Retention period ended (1 Aug|Aug 1,) 2025 — no longer held$/,
    false,
  ],
  // Held with no date is a contradiction the server should never send — but if it does, the
  // record reads as held, never as free to delete.
  [{ status: 'held', expires_on: null }, 'Held — expiry date unavailable', true],
  [
    { status: 'needs_dob', expires_on: null },
    'Retention cannot be computed — add a date of birth',
    true,
  ],
] as const)('the Records line reads %j as %s', async (retention, words, warning) => {
  stubApi({
    signedIn: true,
    dualRole: false,
    respond: (url: string) =>
      url === '/api/customers/c1'
        ? Response.json({ ...PROFILE, customer: { ...PRIYA_DETAIL, retention } })
        : undefined,
  })

  renderApp('/clients/c1')

  const line = await screen.findByText(words)
  expect(line.closest('[data-slot="badge"]') !== null).toBe(warning)
  if (warning) expect(line.closest('[data-slot="badge"]')).toHaveAttribute('data-variant', 'warning')
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

/**
 * Access history (ADR-0002 §6): who opened this record, for holders of `audit.view`. What is
 * worth pinning: the card never appears — and never asks — without the capability; it
 * renders the server's rows on the business's clock; and the two date inputs are the
 * server's range (`from`/`to`), not a filter over what happens to be loaded.
 */

const ACCESS_LOG = {
  entries: [
    {
      id: 7,
      occurred_at: '2026-09-18T14:05:00Z',
      actor_user_id: 'u2',
      actor_name: 'Ana Rossi',
      actor_role: 'Front desk',
      resource_type: 'customer_profile',
      resource_id: 'c1',
      action: 'view',
      ip: '10.0.0.7',
    },
    {
      id: 3,
      occurred_at: '2026-09-01T13:00:00Z',
      actor_user_id: 'u9',
      actor_name: 'u9',
      actor_role: 'Staff',
      resource_type: 'customer_erasure',
      resource_id: 'c1',
      action: 'view',
      ip: null,
    },
  ],
  total: 2,
  from: '2026-06-21',
  to: '2026-09-19',
  timezone: 'America/Toronto',
}

const ME_AUDITOR = {
  ...ME_WITH_MANAGE,
  role: 'Administrator',
  capabilities: ['admin', 'customers.view', 'audit.view'],
  mode: 'admin',
  can_switch_modes: true,
  admin_grant_expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
  admin_hard_limit_at: new Date(Date.now() + 30 * 60_000).toISOString(),
}

function auditor(me: object = ME_AUDITOR) {
  return stubApi({
    signedIn: true,
    dualRole: false,
    respond: (url: string) => {
      const parsed = new URL(url, 'http://test')
      if (url === '/api/auth/me') return Response.json(me)
      if (parsed.pathname === '/api/customers/c1') return Response.json(PROFILE)
      if (parsed.pathname === '/api/admin/customers/c1/access-log') {
        const from = parsed.searchParams.get('from')
        return Response.json(
          from
            ? { ...ACCESS_LOG, entries: [], total: 0, from, to: parsed.searchParams.get('to') }
            : ACCESS_LOG,
        )
      }
      return undefined
    },
  })
}

const reportCalls = (calls: Call[]) => calls.filter((c) => c.url.includes('/access-log'))

test('without audit.view there is no access history card, and nothing asks for one', async () => {
  const { calls } = fake()
  renderApp('/clients/c1')
  await screen.findByRole('heading', { name: 'Priya Nair' })

  expect(screen.queryByRole('heading', { name: 'Access history' })).not.toBeInTheDocument()
  expect(reportCalls(calls)).toHaveLength(0)
})

test('an audit.view holder in Admin Mode sees who opened the record, on the business clock', async () => {
  auditor()
  renderApp('/clients/c1')

  const table = await screen.findByRole('table', { name: 'Access history' })
  const rows = within(table).getAllByRole('row').slice(1)
  expect(rows).toHaveLength(2)
  expect(within(rows[0]).getByText('Ana Rossi')).toBeInTheDocument()
  expect(within(rows[0]).getByText('Front desk')).toBeInTheDocument()
  expect(within(rows[0]).getByText('Opened profile')).toBeInTheDocument()
  expect(within(rows[0]).getByText('10.0.0.7')).toBeInTheDocument()
  // 14:05Z on 18 September is 10:05 in Toronto.
  expect(within(rows[0]).getByText(/10:05/)).toBeInTheDocument()
  // A departed account: the id stands in for the name, and the row is still there.
  expect(within(rows[1]).getByText('u9')).toBeInTheDocument()
  // The erasure POST logs its own row — the one an investigator most wants to read.
  expect(within(rows[1]).getByText('Requested erasure')).toBeInTheDocument()
  expect(screen.getByLabelText('From')).toHaveValue('2026-06-21')
  expect(screen.getByLabelText('To')).toHaveValue('2026-09-19')
})

test('changing the dates asks the server again with from and to', async () => {
  const { calls } = auditor()
  renderApp('/clients/c1')
  const table = await screen.findByRole('table', { name: 'Access history' })
  expect(table.closest('[aria-busy]')).toHaveAttribute('aria-busy', 'false')

  fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-09-10' } })

  // The previous range stays up, marked busy, until the new one arrives.
  expect(table.closest('[aria-busy]')).toHaveAttribute('aria-busy', 'true')

  await waitFor(() => {
    const last = new URL(reportCalls(calls).at(-1)!.url, 'http://test')
    expect(last.searchParams.get('from')).toBe('2026-09-10')
    expect(last.searchParams.get('to')).toBe('2026-09-19')
  })
  expect(await screen.findByText('Nobody opened this record in these dates')).toBeInTheDocument()
})

test('in Staff Mode the card asks for Admin Mode instead of requesting a refusal', async () => {
  const { calls } = auditor({
    ...ME_AUDITOR,
    mode: 'staff',
    admin_grant_expires_at: null,
    admin_hard_limit_at: null,
  })
  renderApp('/clients/c1')

  expect(await screen.findByRole('heading', { name: 'Access history' })).toBeInTheDocument()
  expect(screen.getByText(/Switch to Admin Mode/)).toBeInTheDocument()
  expect(reportCalls(calls)).toHaveLength(0)
})

test('form access history identifies opened PDFs and answers in clinic language', async () => {
  auditor()
  const passthrough = vi.mocked(fetch).getMockImplementation()!
  vi.mocked(fetch).mockImplementation(async (url, init) => String(url).includes('/access-log')
    ? Response.json({ ...ACCESS_LOG, entries: [
      { ...ACCESS_LOG.entries[0], resource_type: 'form_document' },
      { ...ACCESS_LOG.entries[1], resource_type: 'form_submission' },
    ] })
    : passthrough(url, init))
  renderApp('/clients/c1')
  const table = await screen.findByRole('table', { name: 'Access history' })
  expect(within(table).getByText('Opened form PDF')).toBeInTheDocument()
  expect(within(table).getByText('Opened form answers')).toBeInTheDocument()
})
