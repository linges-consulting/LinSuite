import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * Essential forms and compliance (Task 8, #51): the profile's alert banner, its "Send form"
 * shortcut preselecting the missing template, and the Home "Forms needed" card.
 */

afterEach(() => vi.unstubAllGlobals())

const CUSTOMER = {
  id: 'c1',
  first_name: 'Priya',
  last_name: 'Nair',
  email: null,
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

const TEMPLATE_ID = 't1'

function reader(capabilities: string[]) {
  return stubApi({
    signedIn: true,
    dualRole: false,
    respond: (url: string, body: unknown) => {
      const path = new URL(url, 'http://test').pathname
      if (url === '/api/auth/me') {
        return Response.json({
          id: 'u2',
          email: 'desk@cedar.example',
          role: 'Staff',
          capabilities,
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
        })
      }
      if (path === '/api/customers/c1') {
        return Response.json({
          customer: CUSTOMER,
          timezone: 'America/Toronto',
          appointments: [],
          notification_failures: [],
        })
      }
      if (path === '/api/customers/c1/compliance') {
        return Response.json({
          templates: [{ template_id: TEMPLATE_ID, name: 'Massage waiver', status: 'missing' }],
        })
      }
      if (path === '/api/customers/c1/form-links') {
        if (body) {
          return Response.json({
            id: 'link1',
            url: 'https://cedar.example/f/#tokentokentokentokentokentokentoken',
            expires_at: '2026-09-24T12:00:00Z',
            emailed_to: null,
          })
        }
        return Response.json({ links: [] })
      }
      if (path === '/api/customers/c1/forms') return Response.json({ submissions: [] })
      if (path === '/api/forms/templates') {
        return Response.json({
          templates: [{ template_id: TEMPLATE_ID, name: 'Massage waiver', kind: 'waiver', version: 1 }],
        })
      }
      return undefined
    },
  })
}

test('the profile banner lists a missing essential form and opens the send dialog preselected', async () => {
  const { calls } = reader(['customers.view', 'forms.issue'])
  const user = userEvent.setup()
  renderApp('/clients/c1')

  const banner = await screen.findByRole('status', { name: 'Essential forms outstanding' })
  expect(within(banner).getByText('Massage waiver')).toBeInTheDocument()
  expect(within(banner).getByText('Missing')).toBeInTheDocument()

  await user.click(within(banner).getByRole('button', { name: 'Send form' }))
  const dialog = await screen.findByRole('dialog', { name: 'Send a form' })
  // Preselected: the template is already chosen, so Create link needs no picking first.
  expect(within(dialog).getByRole('combobox')).toHaveTextContent('Massage waiver')
  await user.click(within(dialog).getByRole('button', { name: 'Create link' }))

  await waitFor(() => {
    const issued = calls.find((c) => c.method === 'POST' && c.url === '/api/customers/c1/form-links')
    expect(issued?.body).toEqual({ template_id: TEMPLATE_ID })
  })
})

test('a compliant client (no entries) shows no banner at all', async () => {
  const { calls } = stubApi({
    signedIn: true,
    dualRole: false,
    respond: (url: string) => {
      const path = new URL(url, 'http://test').pathname
      if (url === '/api/auth/me') {
        return Response.json({
          id: 'u2',
          email: 'desk@cedar.example',
          role: 'Staff',
          capabilities: ['customers.view'],
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
        })
      }
      if (path === '/api/customers/c1') {
        return Response.json({
          customer: CUSTOMER,
          timezone: 'America/Toronto',
          appointments: [],
          notification_failures: [],
        })
      }
      if (path === '/api/customers/c1/compliance') return Response.json({ templates: [] })
      return undefined
    },
  })
  renderApp('/clients/c1')

  await screen.findByRole('heading', { name: 'Priya Nair' })
  await waitFor(() => expect(calls.some((c) => c.url.endsWith('/compliance'))).toBe(true))
  expect(screen.queryByRole('status', { name: 'Essential forms outstanding' })).not.toBeInTheDocument()
})

// --- Home: "Forms needed" ----------------------------------------------------------------------

function homeStub(capabilities: string[], clients: unknown[]) {
  return stubApi({
    signedIn: true,
    dualRole: false,
    respond: (url: string) => {
      const parsed = new URL(url, 'http://test')
      if (url === '/api/auth/me') {
        return Response.json({
          id: 'u2',
          email: 'desk@cedar.example',
          role: 'Staff',
          capabilities,
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
        })
      }
      if (parsed.pathname === '/api/forms/compliance') return Response.json({ clients })
      return undefined
    },
  })
}

test('the Home "Forms needed" card renders each client with their outstanding forms', async () => {
  const { calls } = homeStub(['forms.issue'], [
    {
      customer_id: 'c1',
      customer_name: 'Priya Nair',
      next_appointment_at: '2026-10-01T14:00:00Z',
      templates: [{ template_id: TEMPLATE_ID, name: 'Massage waiver', status: 'missing' }],
    },
  ])
  renderApp('/')

  const card = (await screen.findByRole('heading', { name: 'Forms needed' })).closest(
    '[data-slot="card"]',
  ) as HTMLElement
  expect(within(card).getByText('Priya Nair')).toBeInTheDocument()
  expect(within(card).getByText('Massage waiver: Missing')).toBeInTheDocument()
  const fetched = calls.find((c) => c.url.startsWith('/api/forms/compliance'))
  expect(new URL(fetched!.url, 'http://test').searchParams.get('days')).toBe('14')
})

test('the Home card shows the empty state when nothing is outstanding', async () => {
  homeStub(['forms.issue'], [])
  renderApp('/')

  const card = (await screen.findByRole('heading', { name: 'Forms needed' })).closest(
    '[data-slot="card"]',
  ) as HTMLElement
  expect(within(card).getByText('Nothing outstanding')).toBeInTheDocument()
})

test('without forms.issue, Home has no "Forms needed" card at all', async () => {
  const { calls } = homeStub(['customers.view'], [])
  renderApp('/')

  await screen.findByText('Your workspace is ready')
  expect(screen.queryByRole('heading', { name: 'Forms needed' })).not.toBeInTheDocument()
  expect(calls.some((c) => c.url.startsWith('/api/forms/compliance'))).toBe(false)
})
