import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * `/manage-booking/#<token>` (Phase 6 Task 6, #10): the client's own way back into a booking
 * made through `/book` — view, cancel, reschedule. Not retested here: the server's own
 * cutoff/toggle enforcement (`test_booking_management.py` already owns that ground) — only
 * that this page reads `ManageBookingOut` correctly and calls the three endpoints with the
 * token from the URL fragment, never a path segment.
 */

const TOKEN = 'tok-abc123'

const BOOKING = {
  appointment_id: 'a1',
  status: 'confirmed',
  starts_at: '2026-01-05T15:00:00Z',
  ends_at: '2026-01-05T16:00:00Z',
  service_id: 's1',
  service_name: 'Swedish Massage',
  staff_id: 'st1',
  staff_name: 'Ana Rossi',
  cancellable: true,
}

const AVAILABILITY_RESPONSE = {
  service_id: 's1',
  timezone: 'America/Toronto',
  granularity_minutes: 15,
  horizon_ends_on: '2026-03-01',
  days: [
    {
      date: '2026-01-06',
      slots: [{ starts_at: '2026-01-06T15:00:00Z', ends_at: '2026-01-06T16:00:00Z', staff_ids: ['st1'] }],
    },
  ],
}

type Answer = { status: number; body: unknown }

function openManage(
  opts: {
    manageResponse?: Answer
    cancelResponse?: Answer
    rescheduleResponse?: Answer
    token?: string
  } = {},
) {
  const {
    manageResponse = { status: 200, body: BOOKING },
    cancelResponse,
    rescheduleResponse,
    token = TOKEN,
  } = opts
  stubApi({ signedIn: false })
  const fetchStub = vi.mocked(fetch)
  const passthrough = fetchStub.getMockImplementation()!
  fetchStub.mockImplementation(async (url, init) => {
    const u = String(url)
    if (u === '/api/public/booking/manage') {
      return Response.json(manageResponse.body, { status: manageResponse.status })
    }
    if (u === '/api/public/booking/manage/cancel' && cancelResponse) {
      return Response.json(cancelResponse.body, { status: cancelResponse.status })
    }
    if (u === '/api/public/booking/manage/reschedule' && rescheduleResponse) {
      return Response.json(rescheduleResponse.body, { status: rescheduleResponse.status })
    }
    if (u.startsWith('/api/public/booking/availability')) return Response.json(AVAILABILITY_RESPONSE)
    return passthrough(url, init)
  })
  renderApp(`/manage-booking/#${token}`)
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

test('a live link shows the booking', async () => {
  openManage()

  expect(await screen.findByText('Your booking')).toBeInTheDocument()
  expect(screen.getByText(/Swedish Massage with Ana Rossi/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Cancel booking' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Reschedule' })).toBeInTheDocument()
})

test('an unknown or dead token is one plain dead end, not a bare error', async () => {
  openManage({ manageResponse: { status: 404, body: { detail: 'Gone', code: 'link_invalid' } } })

  expect(await screen.findByText('This link is no longer valid')).toBeInTheDocument()
  expect(screen.getByText('Please contact us directly.')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Cancel booking' })).not.toBeInTheDocument()
})

test('cancelling asks for confirmation, then cancels and shows the new status', async () => {
  openManage({ cancelResponse: { status: 200, body: { ...BOOKING, status: 'cancelled', cancellable: false } } })
  const user = userEvent.setup()
  await screen.findByText('Your booking')

  await user.click(screen.getByRole('button', { name: 'Cancel booking' }))
  expect(screen.getByText('Cancel this booking? This cannot be undone.')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'No, keep it' }))
  expect(screen.queryByText('Cancel this booking? This cannot be undone.')).not.toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Cancel booking' }))
  await user.click(screen.getByRole('button', { name: 'Yes, cancel' }))

  expect(await screen.findByText('This booking is cancelled.')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Cancel booking' })).not.toBeInTheDocument()
})

test('cancelling inside the cutoff shows a clear inline message, not a dead end', async () => {
  openManage({
    cancelResponse: {
      status: 422,
      body: {
        detail: 'Online cancellation is no longer available for this booking. Please contact us directly.',
        code: 'online_change_not_allowed',
      },
    },
  })
  const user = userEvent.setup()
  await screen.findByText('Your booking')

  await user.click(screen.getByRole('button', { name: 'Cancel booking' }))
  await user.click(screen.getByRole('button', { name: 'Yes, cancel' }))

  expect(
    await screen.findByText(
      'Online cancellation is no longer available for this booking. Please contact us directly.',
    ),
  ).toBeInTheDocument()
  // The booking itself is untouched — still on the ordinary view, not a dead end.
  expect(screen.getByText('Your booking')).toBeInTheDocument()
  expect(screen.getByText(/Swedish Massage with Ana Rossi/)).toBeInTheDocument()
})

test('rescheduling shows a slot picker for the same service and staff, and updates on success', async () => {
  const rescheduled = {
    ...BOOKING,
    starts_at: '2026-01-06T15:00:00Z',
    ends_at: '2026-01-06T16:00:00Z',
  }
  openManage({ rescheduleResponse: { status: 200, body: rescheduled } })
  const user = userEvent.setup()
  await screen.findByText('Your booking')

  await user.click(screen.getByRole('button', { name: 'Reschedule' }))
  expect(await screen.findByText('Pick a new time')).toBeInTheDocument()
  await user.click(await screen.findByRole('button', { name: '10:00 AM' }))
  await user.click(screen.getByRole('button', { name: 'Confirm new time' }))

  expect(await screen.findByText('Your booking')).toBeInTheDocument()
  await waitFor(() => expect(screen.getAllByText(/Swedish Massage with Ana Rossi/).length).toBeGreaterThan(0))
})

test('a not-cancellable booking shows no cancel/reschedule actions', async () => {
  openManage({ manageResponse: { status: 200, body: { ...BOOKING, cancellable: false } } })

  await screen.findByText('Your booking')
  expect(
    screen.getByText('Online changes are no longer available for this booking. Please contact us directly.'),
  ).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Cancel booking' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Reschedule' })).not.toBeInTheDocument()
})
