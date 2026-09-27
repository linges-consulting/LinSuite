import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi, type Call } from './harness'

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

/**
 * Settings → Notifications (Phase 12 Task 6, #11). What is pinned: an unconfigured sender
 * says so plainly, a save sends the whole form but never a secret nobody typed, and each
 * template saves on its own.
 */

const ADMIN = { signedIn: true, dualRole: true, adminWindowMs: 15 * 60_000 }
const NOTIFICATIONS = '/api/admin/business/notifications'

type Template = {
  id: string
  notification_type: string
  channel: 'email' | 'sms'
  subject_template: string | null
  body_template: string
  updated_at: string
  merge_fields: string[]
}

type Settings = {
  email_sender: string | null
  email_ready: boolean
  resend_from_address: string | null
  resend_api_key_set: boolean
  resend_domain_verified_at: string | null
  smtp_host: string | null
  smtp_port: number | null
  smtp_username: string | null
  smtp_from_address: string | null
  smtp_password_set: boolean
  smtp_verified_at: string | null
  sms_enabled: boolean
  sms_ready: boolean
  twilio_account_sid: string | null
  twilio_from_number: string | null
  twilio_auth_token_set: boolean
  reminder_intervals_hours: number[]
  templates: Template[]
  online_booking_enabled: boolean
  online_cancellation_enabled: boolean
  cancellation_cutoff_hours: number
  booking_daily_cap_per_ip: number
  booking_daily_cap_per_email: number
}

const TEMPLATES: Template[] = [
  {
    id: 't-email',
    notification_type: 'booking_confirmation',
    channel: 'email',
    subject_template: 'Your appointment with $business_name is confirmed',
    body_template: 'Hi $client_name, see you at $appointment_time.',
    updated_at: '2026-01-01T00:00:00Z',
    merge_fields: ['$business_name', '$client_name', '$appointment_time'],
  },
  {
    id: 't-sms',
    notification_type: 'booking_confirmation',
    channel: 'sms',
    subject_template: null,
    body_template: 'Hi $client_name, confirmed for $appointment_time.',
    updated_at: '2026-01-01T00:00:00Z',
    merge_fields: ['$client_name', '$appointment_time'],
  },
]

function fakeNotifications(initial: Partial<Settings> = {}) {
  let settings: Settings = {
    email_sender: null,
    email_ready: false,
    resend_from_address: null,
    resend_api_key_set: false,
    resend_domain_verified_at: null,
    smtp_host: null,
    smtp_port: null,
    smtp_username: null,
    smtp_from_address: null,
    smtp_password_set: false,
    smtp_verified_at: null,
    sms_enabled: false,
    sms_ready: false,
    twilio_account_sid: null,
    twilio_from_number: null,
    twilio_auth_token_set: false,
    reminder_intervals_hours: [24, 2],
    templates: TEMPLATES,
    online_booking_enabled: true,
    online_cancellation_enabled: true,
    cancellation_cutoff_hours: 24,
    booking_daily_cap_per_ip: 20,
    booking_daily_cap_per_email: 5,
    ...initial,
  }
  const fake = stubApi({
    ...ADMIN,
    respond: (url, body) => {
      if (url === NOTIFICATIONS) {
        if (body !== undefined) {
          // Same shape the real PATCH handler uses: a secret key present and truthy replaces
          // the stored flag, absent leaves it alone — never re-derived from the raw value.
          const { resend_api_key, smtp_password, twilio_auth_token, ...rest } = body
          settings = {
            ...settings,
            ...rest,
            resend_api_key_set: resend_api_key ? true : settings.resend_api_key_set,
            smtp_password_set: smtp_password ? true : settings.smtp_password_set,
            twilio_auth_token_set: twilio_auth_token ? true : settings.twilio_auth_token_set,
          }
        }
        return Response.json(settings)
      }
      if (url === `${NOTIFICATIONS}/test-email`) {
        settings = {
          ...settings,
          resend_domain_verified_at: '2026-01-02T00:00:00Z',
          smtp_verified_at: '2026-01-02T00:00:00Z',
          email_ready: true,
        }
        return Response.json(settings)
      }
      if (url === `${NOTIFICATIONS}/test-sms`) {
        return Response.json({ sent: true })
      }
      if (url.startsWith(`${NOTIFICATIONS}/templates/`)) {
        const id = url.split('/').pop()
        settings = {
          ...settings,
          templates: settings.templates.map((t) => (t.id === id ? { ...t, ...body } : t)),
        }
        return Response.json(settings.templates.find((t) => t.id === id))
      }
      return undefined
    },
  })
  return fake
}

const patches = (calls: Call[]) =>
  calls.filter((c) => c.url === NOTIFICATIONS && c.method === 'PATCH')

async function openNotifications() {
  const user = userEvent.setup()
  renderApp('/settings')
  await user.click(await screen.findByRole('tab', { name: 'Notifications' }))
  return user
}

test('an unconfigured sender says so plainly', async () => {
  fakeNotifications()
  await openNotifications()

  expect(await screen.findByText(/No email sender is configured/)).toBeInTheDocument()
  expect(screen.getByText('Not configured')).toBeInTheDocument()
})

test('choosing Resend and saving sends the whole form, including the new key', async () => {
  const { calls } = fakeNotifications()
  const user = await openNotifications()

  await user.click(await screen.findByRole('combobox', { name: 'Sender' }))
  await user.click(await screen.findByRole('option', { name: 'Resend' }))
  await user.type(await screen.findByLabelText('From address'), 'hello@cedar.example')
  await user.type(screen.getByLabelText('API key'), 're_live_key')
  await user.click(screen.getByRole('button', { name: 'Save changes' }))

  await waitFor(() => expect(patches(calls)).toHaveLength(1))
  const body = patches(calls)[0].body as Record<string, unknown>
  expect(body.email_sender).toBe('resend')
  expect(body.resend_from_address).toBe('hello@cedar.example')
  expect(body.resend_api_key).toBe('re_live_key')
  expect(await screen.findByText('Notification settings saved')).toBeInTheDocument()
})

test('a stored secret is never retyped to keep it', async () => {
  const { calls } = fakeNotifications({
    email_sender: 'resend',
    resend_from_address: 'hello@cedar.example',
    resend_api_key_set: true,
  })
  const user = await openNotifications()

  await user.clear(await screen.findByLabelText('From address'))
  await user.type(screen.getByLabelText('From address'), 'new@cedar.example')
  await user.click(screen.getByRole('button', { name: 'Save changes' }))

  await waitFor(() => expect(patches(calls)).toHaveLength(1))
  const body = patches(calls)[0].body as Record<string, unknown>
  expect(body.resend_from_address).toBe('new@cedar.example')
  expect('resend_api_key' in body).toBe(false)
})

test('a successful test email flips the readiness badge', async () => {
  fakeNotifications({
    email_sender: 'resend',
    resend_from_address: 'hello@cedar.example',
    resend_api_key_set: true,
  })
  const user = await openNotifications()
  expect(screen.getByText('Not verified yet')).toBeInTheDocument()

  await user.type(await screen.findByLabelText('Send a test email to'), 'owner@cedar.example')
  await user.click(screen.getByRole('button', { name: 'Send test email' }))

  await waitFor(() => expect(screen.getByText('Ready')).toBeInTheDocument())
  expect(await screen.findByText(/Test email sent to owner@cedar.example/)).toBeInTheDocument()
});

test('turning on SMS reveals the Twilio fields and a test action', async () => {
  const { calls } = fakeNotifications()
  const user = await openNotifications()

  await user.click(await screen.findByRole('checkbox', { name: 'Send text messages' }))
  await user.type(screen.getByLabelText('Twilio Account SID'), 'ACtest')
  await user.type(screen.getByLabelText('Twilio Auth Token'), 'authtoken')
  await user.type(screen.getByLabelText('From number'), '+15551234567')
  await user.click(screen.getByRole('button', { name: 'Save changes' }))

  await waitFor(() => expect(patches(calls)).toHaveLength(1))
  const body = patches(calls)[0].body as Record<string, unknown>
  expect(body.sms_enabled).toBe(true)
  expect(body.twilio_account_sid).toBe('ACtest')
  expect(body.twilio_auth_token).toBe('authtoken')
});

test('a reminder-interval validation error surfaces under the field', async () => {
  const defaults = {
    email_sender: null,
    email_ready: false,
    resend_from_address: null,
    resend_api_key_set: false,
    resend_domain_verified_at: null,
    smtp_host: null,
    smtp_port: null,
    smtp_username: null,
    smtp_from_address: null,
    smtp_password_set: false,
    smtp_verified_at: null,
    sms_enabled: false,
    sms_ready: false,
    twilio_account_sid: null,
    twilio_from_number: null,
    twilio_auth_token_set: false,
    reminder_intervals_hours: [24, 2],
    templates: TEMPLATES,
    online_booking_enabled: true,
    online_cancellation_enabled: true,
    cancellation_cutoff_hours: 24,
    booking_daily_cap_per_ip: 20,
    booking_daily_cap_per_email: 5,
  }
  const { calls } = stubApi({
    ...ADMIN,
    respond: (url, body) => {
      if (url !== NOTIFICATIONS) return undefined
      if (body === undefined) return Response.json(defaults)
      return Response.json(
        {
          detail: [
            {
              type: 'value_error',
              loc: ['body', 'reminder_intervals_hours'],
              msg: 'Value error, Set at least one reminder interval.',
            },
          ],
        },
        { status: 422 },
      )
    },
  })
  const user = await openNotifications()

  const intervals = await screen.findByLabelText(
    /Send reminders this many hours before an appointment/,
  )
  await user.clear(intervals)
  await user.click(screen.getByRole('button', { name: 'Save changes' }))

  expect(await screen.findByText('Set at least one reminder interval.')).toBeInTheDocument()
  expect(calls.filter((c) => c.url === NOTIFICATIONS && c.method === 'PATCH')).toHaveLength(1)
});

test('a template saves on its own, independently of the sender form', async () => {
  const { calls } = fakeNotifications()
  const user = await openNotifications()

  const body = await screen.findByLabelText('Message', { selector: '#template-body-t-email' })
  const saveButtons = screen.getAllByRole('button', { name: 'Save template' })
  expect(saveButtons[0]).toBeDisabled()

  await user.clear(body)
  await user.type(body, 'Hi $client_name, updated body.')
  expect(saveButtons[0]).toBeEnabled()
  await user.click(saveButtons[0])

  expect(await screen.findByText('Template saved')).toBeInTheDocument()
  expect(calls).toContainEqual(
    expect.objectContaining({ url: `${NOTIFICATIONS}/templates/t-email`, method: 'PUT' }),
  )
  // The sender form was never touched or submitted by saving a template.
  expect(patches(calls)).toHaveLength(0)
});

// --- booking-portal policy (Phase 6 Task 4, #10) --------------------------------------------

test('turning off online booking is sent on save', async () => {
  const { calls } = fakeNotifications()
  const user = await openNotifications()

  await user.click(await screen.findByRole('checkbox', { name: 'Allow clients to book online' }))
  await user.click(screen.getByRole('button', { name: 'Save changes' }))

  await waitFor(() => expect(patches(calls)).toHaveLength(1))
  const body = patches(calls)[0].body as Record<string, unknown>
  expect(body.online_booking_enabled).toBe(false)
})

test('editing the daily caps and cancellation cutoff sends numbers, not strings', async () => {
  const { calls } = fakeNotifications()
  const user = await openNotifications()

  const perIp = await screen.findByLabelText('Per-IP daily booking limit')
  await user.clear(perIp)
  await user.type(perIp, '50')
  const cutoff = screen.getByLabelText(/Cancellation cutoff/)
  await user.clear(cutoff)
  await user.type(cutoff, '48')
  await user.click(screen.getByRole('button', { name: 'Save changes' }))

  await waitFor(() => expect(patches(calls)).toHaveLength(1))
  const body = patches(calls)[0].body as Record<string, unknown>
  expect(body.booking_daily_cap_per_ip).toBe(50)
  expect(body.cancellation_cutoff_hours).toBe(48)
})

test('turning off online cancellation is sent on save', async () => {
  const { calls } = fakeNotifications()
  const user = await openNotifications()

  await user.click(
    await screen.findByRole('checkbox', {
      name: 'Allow clients to cancel or reschedule online',
    }),
  )
  await user.click(screen.getByRole('button', { name: 'Save changes' }))

  await waitFor(() => expect(patches(calls)).toHaveLength(1))
  const body = patches(calls)[0].body as Record<string, unknown>
  expect(body.online_cancellation_enabled).toBe(false)
})
