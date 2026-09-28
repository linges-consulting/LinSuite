import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { toast } from 'sonner'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi } from './harness'

/**
 * `/book` (Phase 6 Task 6, #10): the public booking flow, end to end against a stubbed
 * `fetch` — service, provider, slot, identity (with the honeypot), then the confirmation and
 * its management link. Not retested here: the server's own bookability/abuse-control logic
 * (`test_booking_public_create.py` and siblings already own that ground) — only that this
 * page calls the right endpoints in the right order and reads their answers correctly.
 *
 * **`selectOption` opens a trigger by keyboard, not a second click** (`trigger.focus()` +
 * `{ArrowDown}`), found necessary while writing this file: a Radix `Select` on a page with no
 * `Dialog` around it (unlike every existing Select test in this repo, which happens to open
 * one from inside a booking/settings dialog) — mounted a second, separate time in the same
 * jsdom document via any extra context provider at all (`QueryClientProvider`,
 * `MemoryRouter`, `sonner`'s `Toaster`, tried independently) — stops responding to a second
 * `user.click()` on its own trigger, `data-state` staying `closed` indefinitely; opening the
 * same trigger by keyboard instead works every time. Filed as a real, narrow interaction
 * between Radix's pointer-based open handler and this jsdom/React/testing-library
 * combination, not chased into Radix's own source — this is the workaround, applied
 * uniformly rather than only where the failure was first observed, so an unrelated later
 * test in this file never depends on running first.
 */

const SERVICE = {
  id: 's1',
  name: 'Swedish Massage',
  description: null,
  duration_minutes: 60,
  price_cents: 12000,
  staff: [{ id: 'st1', display_name: 'Ana Rossi' }],
}

const SERVICES_RESPONSE = { online_booking_enabled: true, services: [SERVICE] }

// 15:00Z is 10:00 AM in America/Toronto (EST, no DST in January) — the slot list is shown
// through `SlotButtons` with the business's own timezone, so this is deterministic
// regardless of the machine the test runs on.
const SLOT = { starts_at: '2026-01-05T15:00:00Z', ends_at: '2026-01-05T16:00:00Z', staff_ids: ['st1'] }
const AVAILABILITY_RESPONSE = {
  service_id: SERVICE.id,
  timezone: 'America/Toronto',
  granularity_minutes: 15,
  horizon_ends_on: '2026-03-01',
  days: [{ date: '2026-01-05', slots: [SLOT] }],
}

const BOOKING = {
  appointment_id: 'a1',
  starts_at: SLOT.starts_at,
  ends_at: SLOT.ends_at,
  service_name: SERVICE.name,
  staff_name: 'Ana Rossi',
  management_link: 'https://cedar.example/manage-booking/#tok-abc123',
}

type BookingAnswer = { status: number; body: unknown }

function openBooking(
  opts: {
    servicesResponse?: unknown
    bookingResponse?: BookingAnswer
    path?: string
  } = {},
) {
  const {
    servicesResponse = SERVICES_RESPONSE,
    bookingResponse = { status: 201, body: BOOKING },
    path = '/book',
  } = opts
  const sent: Record<string, unknown>[] = []
  stubApi({ signedIn: false })
  const fetchStub = vi.mocked(fetch)
  const passthrough = fetchStub.getMockImplementation()!
  fetchStub.mockImplementation(async (url, init) => {
    const u = String(url)
    if (u === '/api/public/booking/services') return Response.json(servicesResponse)
    if (u.startsWith('/api/public/booking/availability')) return Response.json(AVAILABILITY_RESPONSE)
    if (u === '/api/public/booking' && (init?.method ?? 'GET') === 'POST') {
      sent.push(JSON.parse(String(init!.body)))
      return Response.json(bookingResponse.body, { status: bookingResponse.status })
    }
    return passthrough(url, init)
  })
  renderApp(path)
  return sent
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/** Opens `comboboxName`'s listbox by keyboard (see the file docstring) and picks the option
 *  named `optionName`. */
async function selectOption(
  user: ReturnType<typeof userEvent.setup>,
  comboboxName: string,
  optionName: string | RegExp,
) {
  const trigger = await screen.findByRole('combobox', { name: comboboxName })
  trigger.focus()
  await user.keyboard('{ArrowDown}')
  await user.click(await screen.findByRole('option', { name: optionName }))
}

async function pickSlot(user: ReturnType<typeof userEvent.setup>) {
  await selectOption(user, 'Service', SERVICE.name)
  await user.click(await screen.findByRole('button', { name: '10:00 AM' }))
  await user.click(screen.getByRole('button', { name: 'Continue' }))
}

test('the full happy path: pick a service and slot, identify, and see the confirmation with the management link', async () => {
  const sent = openBooking()
  const user = userEvent.setup()
  vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue(undefined)

  await pickSlot(user)

  await user.type(screen.getByLabelText('First name'), 'Priya')
  await user.type(screen.getByLabelText('Last name'), 'Nair')
  await user.type(screen.getByLabelText('Email'), 'priya@example.com')
  await user.click(screen.getByRole('button', { name: 'Confirm booking' }))

  await waitFor(() => expect(sent).toHaveLength(1))
  expect(sent[0]).toMatchObject({
    service_id: SERVICE.id,
    staff_id: null,
    starts_at: SLOT.starts_at,
    customer: { first_name: 'Priya', last_name: 'Nair', email: 'priya@example.com', phone: null },
    website: '',
  })

  expect(await screen.findByText("You're booked")).toBeInTheDocument()
  expect(screen.getByText(/Swedish Massage with Ana Rossi/)).toBeInTheDocument()
  const link = screen.getByLabelText('Manage this booking') as HTMLInputElement
  expect(link.value).toBe(BOOKING.management_link)

  const toastSuccess = vi.spyOn(toast, 'success')
  await user.click(screen.getByRole('button', { name: 'Copy' }))
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith(BOOKING.management_link)
  expect(toastSuccess).toHaveBeenCalledWith('Link copied')
})

test('picking a named provider sends that staff id, not "any available"', async () => {
  const sent = openBooking()
  const user = userEvent.setup()

  await selectOption(user, 'Service', SERVICE.name)
  await selectOption(user, 'Provider', 'Ana Rossi')
  await user.click(await screen.findByRole('button', { name: '10:00 AM' }))
  await user.click(screen.getByRole('button', { name: 'Continue' }))
  await user.type(screen.getByLabelText('First name'), 'Priya')
  await user.type(screen.getByLabelText('Last name'), 'Nair')
  await user.type(screen.getByLabelText('Phone'), '4165550199')
  await user.click(screen.getByRole('button', { name: 'Confirm booking' }))

  await waitFor(() => expect(sent).toHaveLength(1))
  expect(sent[0]).toMatchObject({ staff_id: 'st1' })
})

test('the honeypot field is in the form but invisible to a sighted user', async () => {
  openBooking()
  const user = userEvent.setup()
  await pickSlot(user)

  const honeypot = document.querySelector('input[name="website"]') as HTMLInputElement
  expect(honeypot).toBeInTheDocument()
  expect(honeypot).toHaveAttribute('tabindex', '-1')
  // Off-screen, not `display: none`/`visibility: hidden` — a real browser paints nothing
  // at this position, but the element still has layout, which is what lets a bot's naive
  // form-filler still find and populate it. Both the wrapper's `aria-hidden` and the
  // positioning keep a sighted or assistive-tech visitor from ever reaching it.
  const wrapper = honeypot.closest('[aria-hidden="true"]') as HTMLElement
  expect(wrapper).toBeInTheDocument()
  expect(wrapper.style.position).toBe('absolute')
  expect(wrapper.style.left).toBe('-9999px')
  // A sighted user tabbing through the form never lands on it, and it has no visible label
  // in the accessible name a screen reader would announce during normal navigation either.
  expect(screen.queryByRole('textbox', { name: /website/i })).not.toBeInTheDocument()
})

test('a booking-disabled response is shown clearly', async () => {
  openBooking({ servicesResponse: { online_booking_enabled: false, services: [] } })

  expect(await screen.findByText("Booking isn't available right now")).toBeInTheDocument()
  expect(screen.queryByRole('combobox', { name: 'Service' })).not.toBeInTheDocument()
})

test('a slot taken between picking and confirming sends the visitor back to pick another', async () => {
  const sent = openBooking({
    bookingResponse: { status: 409, body: { detail: 'Taken', code: 'slot_taken' } },
  })
  const user = userEvent.setup()
  await pickSlot(user)
  await user.type(screen.getByLabelText('First name'), 'Priya')
  await user.type(screen.getByLabelText('Last name'), 'Nair')
  await user.type(screen.getByLabelText('Email'), 'priya@example.com')
  await user.click(screen.getByRole('button', { name: 'Confirm booking' }))

  await waitFor(() => expect(sent).toHaveLength(1))
  // Back on the picking screen, not stuck on a dead identity form.
  expect(await screen.findByRole('combobox', { name: 'Service' })).toBeInTheDocument()
})

test('?embed=1 hides the "powered by" line', async () => {
  openBooking({ path: '/book?embed=1' })
  await screen.findByRole('combobox', { name: 'Service' })

  expect(screen.queryByText('Powered by LinSuite')).not.toBeInTheDocument()
})

test('non-embedded, the "powered by" line is shown', async () => {
  openBooking()
  expect(await screen.findByText('Powered by LinSuite')).toBeInTheDocument()
})
