import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi, BUSINESS_NAME } from './harness'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/**
 * Secure form links (#46). The staff side: the profile's Forms card sends a form and shows
 * the link once — Copy, a QR code for the clinic tablet, and "Emailed to …" when the server
 * queued one — with the open links listed and revocable. The client side: `/f/#<token>`
 * renders the pinned schema for a signed-in and a signed-out browser alike, hides a
 * conditional field until its condition holds, and says plainly when a link is dead.
 */

const PREGNANT = '11111111-1111-4111-8111-111111111111'
const WEEKS = '22222222-2222-4222-8222-222222222222'
const TOKEN = 'k'.repeat(43)
const URL_ = `http://localhost/f/#${TOKEN}`

const PUBLIC_FORM = {
  version_id: 'v1',
  template_name: 'Prenatal intake',
  schema: {
    fields: [
      { key: PREGNANT, type: 'yes_no', label: 'Are you pregnant?', required: true },
      {
        key: WEEKS,
        type: 'short_text',
        label: 'How many weeks?',
        required: true,
        show_if: { key: PREGNANT, equals: ['yes'] },
      },
    ],
  },
  business: { name: BUSINESS_NAME, logo_url: null },
  client_first_name: 'Priya',
  expires_at: '2026-09-24T12:00:00Z',
}

const CUSTOMER = {
  id: 'c1',
  first_name: 'Priya',
  last_name: 'Nair',
  email: 'priya@example.com',
  phone: '4165550199',
  created_at: '2026-01-05T15:00:00Z',
  classification: 'new',
  date_of_birth: null,
  emergency_contact_name: null,
  emergency_contact_phone: null,
  emergency_contact_relationship: null,
  secondary_contact_name: null,
  secondary_contact_phone: null,
  secondary_contact_email: null,
  notes: null,
  updated_at: '2026-01-05T15:00:00Z',
  retention: { status: 'not_held', expires_on: null },
  suppressed: false,
  erasure: null,
}

const DESK = {
  id: 'u2',
  email: 'desk@cedar.example',
  role: 'Staff',
  capabilities: ['schedule.view', 'customers.view', 'customers.manage', 'forms.issue'],
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

function staffDesk() {
  let open = [
    {
      id: 'l0',
      template_name: 'Consent to massage',
      version: 2,
      issued_at: '2026-09-21T14:00:00Z',
      expires_at: '2026-09-23T14:00:00Z',
    },
  ]
  return stubApi({
    signedIn: true,
    dualRole: false,
    respond: (url: string) => {
      const path = new URL(url, 'http://test').pathname
      if (url === '/api/auth/me') return Response.json(DESK)
      if (path === '/api/customers/c1') {
        return Response.json({
          customer: CUSTOMER,
          timezone: 'America/Toronto',
          appointments: [],
          notification_failures: [],
        })
      }
      if (path === '/api/forms/templates') {
        return Response.json({
          templates: [{ template_id: 't1', name: 'Prenatal intake', kind: 'intake', version: 1 }],
        })
      }
      if (path === '/api/customers/c1/form-links/l0/revoke') {
        open = []
        return new Response(null, { status: 204 })
      }
      if (path === '/api/customers/c1/form-links') {
        return Response.json({ links: open })
      }
      return undefined
    },
  })
}

test('sending a form shows the link once, with Copy, a QR code and the emailed note', async () => {
  const { calls } = staffDesk()
  // The issue POST, answered here because `respond` does not see the method.
  const fetchStub = vi.mocked(fetch)
  const passthrough = fetchStub.getMockImplementation()!
  fetchStub.mockImplementation(async (url, init) => {
    if (url === '/api/customers/c1/form-links' && init?.method === 'POST') {
      calls.push({ url: String(url), method: 'POST', body: JSON.parse(String(init.body)) })
      return Response.json(
        { id: 'l1', url: URL_, expires_at: '2026-09-24T12:00:00Z', emailed_to: 'priya@example.com' },
        { status: 201 },
      )
    }
    return passthrough(url, init)
  })
  const user = userEvent.setup()
  renderApp('/clients/c1')

  const card = (await screen.findByRole('heading', { name: 'Forms' })).closest('[data-slot="card"]')!
  expect(await within(card as HTMLElement).findByText('Consent to massage')).toBeInTheDocument()
  await user.click(within(card as HTMLElement).getByRole('button', { name: 'Send form' }))
  const dialog = await screen.findByRole('dialog', { name: 'Send a form' })
  await user.click(within(dialog).getByRole('combobox', { name: 'Form' }))
  await user.click(await screen.findByRole('option', { name: 'Prenatal intake' }))
  await user.click(within(dialog).getByRole('button', { name: 'Create link' }))

  expect(await within(dialog).findByRole('status')).toHaveTextContent('Emailed to priya@example.com.')
  expect(within(dialog).getByRole('textbox', { name: 'Form link' })).toHaveValue(URL_)
  expect(within(dialog).getByRole('img', { name: 'QR code for the form link' })).toBeInTheDocument()
  expect(within(dialog).getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  expect(within(dialog).getByText(/Revoke a link the client abandons/)).toBeInTheDocument()
  expect(calls.find((c) => c.method === 'POST')?.body).toEqual({ template_id: 't1' })
})

test('without an email on file the dialog says to copy or scan instead', async () => {
  const { calls } = staffDesk()
  const fetchStub = vi.mocked(fetch)
  const passthrough = fetchStub.getMockImplementation()!
  fetchStub.mockImplementation(async (url, init) => {
    if (url === '/api/customers/c1/form-links' && init?.method === 'POST') {
      calls.push({ url: String(url), method: 'POST' })
      return Response.json(
        { id: 'l1', url: URL_, expires_at: '2026-09-24T12:00:00Z', emailed_to: null },
        { status: 201 },
      )
    }
    return passthrough(url, init)
  })
  const user = userEvent.setup()
  renderApp('/clients/c1')

  await user.click(await screen.findByRole('button', { name: 'Send form' }))
  const dialog = await screen.findByRole('dialog', { name: 'Send a form' })
  await user.click(within(dialog).getByRole('combobox', { name: 'Form' }))
  await user.click(await screen.findByRole('option', { name: 'Prenatal intake' }))
  await user.click(within(dialog).getByRole('button', { name: 'Create link' }))

  expect(await within(dialog).findByRole('status')).toHaveTextContent(/No email on file/)
  expect(within(dialog).getByRole('img', { name: 'QR code for the form link' })).toBeInTheDocument()
})

test('revoking an open link removes it from the list', async () => {
  const { calls } = staffDesk()
  const user = userEvent.setup()
  renderApp('/clients/c1')

  await user.click(await screen.findByRole('button', { name: 'Revoke Consent to massage' }))

  await waitFor(() =>
    expect(calls.some((c) => c.url.endsWith('/l0/revoke') && c.method === 'POST')).toBe(true),
  )
  expect(await screen.findByText('No forms waiting to be filled in.')).toBeInTheDocument()
})

test('staff can enlarge the QR code for a separate unsigned tablet', async () => {
  staffDesk()
  const passthrough = vi.mocked(fetch).getMockImplementation()!
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (url === '/api/customers/c1/form-links' && init?.method === 'POST')
      return Response.json({ id: 'l1', url: URL_, expires_at: '2099-09-24T12:00:00Z', emailed_to: null })
    return passthrough(url, init)
  })
  const user = userEvent.setup()
  renderApp('/clients/c1')
  await user.click(await screen.findByRole('button', { name: 'Send form' }))
  const dialog = await screen.findByRole('dialog', { name: 'Send a form' })
  await user.click(within(dialog).getByRole('combobox', { name: 'Form' }))
  await user.click(await screen.findByRole('option', { name: 'Prenatal intake' }))
  await user.click(within(dialog).getByRole('button', { name: 'Create link' }))
  await user.click(await screen.findByRole('button', { name: 'Show tablet QR' }))
  const tablet = await screen.findByRole('dialog', { name: 'Fill in the clinic' })
  expect(within(tablet).getByRole('img', { name: 'Tablet QR code for the form link' })).toBeInTheDocument()
  expect(within(tablet).getByText(/tablet that is signed out/)).toBeInTheDocument()
  expect(vi.mocked(fetch).mock.calls.some(([url]) => url === '/api/auth/logout')).toBe(false)
})

// --- /f/#<token> ------------------------------------------------------------------------------

/** Open the page the way a browser does: the real address bar carries the fragment too, so
 *  the test can prove it is cleared from there and not only from the router's copy. */
function openLink(path: string) {
  window.history.replaceState(null, '', path)
  renderApp(path)
}

const lookups = () =>
  vi.mocked(fetch).mock.calls.filter(([url]) => String(url).startsWith('/api/public/'))

function publicLink(answer: () => Response, signedIn: boolean) {
  return stubApi({
    signedIn,
    respond: (url: string) => (url === '/api/public/forms/lookup' ? answer() : undefined),
  })
}

for (const signedIn of [true, false]) {
  test(`the form page renders the pinned schema ${signedIn ? 'in a signed-in' : 'in a signed-out'} browser`, async () => {
    publicLink(() => Response.json(PUBLIC_FORM), signedIn)
    const user = userEvent.setup()
    openLink(`/f/#${TOKEN}`)

    expect(await screen.findByRole('heading', { name: 'Prenatal intake' })).toBeInTheDocument()
    expect(screen.getByText('Hi Priya,')).toBeInTheDocument()
    expect(screen.getByText(BUSINESS_NAME)).toBeInTheDocument()
    // No shell: nothing of the staff app is reachable from here.
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()

    expect(screen.queryByLabelText(/How many weeks/)).not.toBeInTheDocument()
    await user.click(screen.getByRole('radio', { name: 'Yes' }))
    expect(screen.getByLabelText(/How many weeks/)).toBeInTheDocument()

    // The token travels in a JSON body — never a path an access log would write down — with
    // no staff cookie, and a referrer policy that still lets the browser send `Origin`.
    const lookup = vi
      .mocked(fetch)
      .mock.calls.find(([url]) => String(url).startsWith('/api/public/'))!
    expect(lookup[0]).toBe('/api/public/forms/lookup')
    expect(lookup[1]).toMatchObject({ method: 'POST', credentials: 'omit', referrerPolicy: 'origin' })
    expect(JSON.parse(String(lookup[1]!.body))).toEqual({ token: TOKEN })
    // The fragment is read once and cleared from the address bar and this history entry: the
    // next person on a shared tablet cannot go back to it. It lives on only in memory.
    expect(window.location.pathname).toBe('/f/')
    expect(window.location.hash).toBe('')
    expect(window.location.href).not.toContain(TOKEN)
  })
}

test('a dead link says so and names the business to contact', async () => {
  publicLink(
    () => Response.json({ detail: 'This link is no longer valid.', code: 'link_invalid' }, { status: 404 }),
    false,
  )
  openLink(`/f/#${TOKEN}`)

  expect(await screen.findByRole('heading', { name: 'This link is no longer valid' })).toBeInTheDocument()
  expect(await screen.findByText(`Please contact ${BUSINESS_NAME} for a new one.`)).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: 'Sign in' })).not.toBeInTheDocument()
})

for (const [what, path] of [
  ['an empty fragment', '/f/'],
  ['the old path form', `/f/${TOKEN}`],
] as const) {
  test(`${what} is a dead link, and nothing is looked up`, async () => {
    publicLink(() => Response.json(PUBLIC_FORM), false)
    openLink(path)

    expect(await screen.findByRole('heading', { name: 'This link is no longer valid' })).toBeInTheDocument()
    expect(lookups()).toHaveLength(0)
    expect(window.location.href).not.toContain(TOKEN)
  })
}

test('the document asks for no referrer before any script runs', async () => {
  const html = await import('../index.html?raw')
  expect(html.default).toContain('<meta name="referrer" content="no-referrer" />')
})
