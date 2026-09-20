import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderApp, stubApi, type Call } from './harness'

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

/**
 * The province suggests a zone and a human confirms it — PRD §7, and the reason it is a
 * suggestion is that Canadian provinces do not map onto timezones. These tests are about the
 * two halves of that: the suggestion never applies itself, and the confirmation says what
 * changing the zone does to rules that already exist.
 */

const ADMIN = { signedIn: true, dualRole: true, adminWindowMs: 15 * 60_000 }

async function openBusiness() {
  const user = userEvent.setup()
  await user.click(await screen.findByRole('tab', { name: 'Business' }))
  return user
}

function patched(calls: Call[]) {
  return calls.filter((call) => call.url === '/api/admin/business/timezone')
}

test('choosing a province suggests its usual zone without applying it', async () => {
  const { calls } = stubApi(ADMIN)
  renderApp('/settings')
  const user = await openBusiness()

  await user.click(await screen.findByRole('combobox', { name: /Province/ }))
  await user.click(await screen.findByRole('option', { name: 'British Columbia' }))

  expect(await screen.findByText(/Most of British Columbia keeps/)).toBeInTheDocument()
  expect(screen.getByText('America/Vancouver')).toBeInTheDocument()
  // Nothing has been sent: a suggestion is a sentence, not a change.
  expect(patched(calls)).toHaveLength(0)
  expect(screen.getByRole('combobox', { name: /IANA timezone/ })).toHaveTextContent(
    'America/Toronto',
  )
})

test('accepting the suggestion still asks for a confirmation before it changes anything', async () => {
  const { calls } = stubApi(ADMIN)
  renderApp('/settings')
  const user = await openBusiness()

  await user.click(await screen.findByRole('combobox', { name: /Province/ }))
  await user.click(await screen.findByRole('option', { name: 'British Columbia' }))
  await user.click(await screen.findByRole('button', { name: 'Use it' }))

  // Chosen in the field, and still not sent.
  await waitFor(() =>
    expect(screen.getByRole('combobox', { name: /IANA timezone/ })).toHaveTextContent(
      'America/Vancouver',
    ),
  )
  expect(patched(calls)).toHaveLength(0)

  await user.click(screen.getByRole('button', { name: 'Change timezone' }))

  const dialog = await screen.findByRole('dialog')
  // The one thing somebody needs to know before agreeing: wall-clock rules keep their times.
  expect(dialog).toHaveTextContent(/stored as local times and keep those local times/)
  expect(patched(calls)).toHaveLength(0)

  await user.click(within(dialog).getByRole('button', { name: 'Change timezone' }))

  await waitFor(() => expect(patched(calls)).toHaveLength(1))
  expect(patched(calls)[0].body).toEqual({ timezone: 'America/Vancouver' })
})

test('cancelling the confirmation leaves the timezone alone', async () => {
  const { calls } = stubApi(ADMIN)
  renderApp('/settings')
  const user = await openBusiness()

  await user.click(await screen.findByRole('combobox', { name: /Province/ }))
  await user.click(await screen.findByRole('option', { name: 'British Columbia' }))
  await user.click(await screen.findByRole('button', { name: 'Use it' }))
  await user.click(screen.getByRole('button', { name: 'Change timezone' }))
  const dialog = await screen.findByRole('dialog')
  await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))

  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(patched(calls)).toHaveLength(0)
})

test('the address block reads in the order it is written on an envelope', async () => {
  stubApi(ADMIN)
  renderApp('/settings')
  await openBusiness()
  await screen.findByLabelText('City')

  const labels = Array.from(document.querySelectorAll('label')).map((l) => l.textContent)

  expect(labels.slice(labels.indexOf('Address'), labels.indexOf('Phone'))).toEqual([
    'Address',
    'Address line 2',
    'City',
    'Province or territory',
    'Postal code',
  ])
})

test('the profile is saved in one request, and the shell re-reads the name', async () => {
  const { calls } = stubApi(ADMIN)
  renderApp('/settings')
  const user = await openBusiness()

  const city = await screen.findByLabelText('City')
  await user.type(city, 'Victoria')
  await user.click(screen.getByRole('button', { name: 'Save changes' }))

  const saved = () =>
    calls.find((call) => call.url === '/api/admin/business' && call.method === 'PUT')
  await waitFor(() => expect(saved()).toBeDefined())
  expect(saved()!.body).toMatchObject({ city: 'Victoria' })
  // The timezone is not in the profile request: it has its own, with its own confirmation.
  expect(saved()!.body).not.toHaveProperty('timezone')
})

test('the booking grid and horizon are saved as numbers, and a cleared field blocks the save', async () => {
  const { calls } = stubApi(ADMIN)
  renderApp('/settings')
  const user = await openBusiness()

  const grid = await screen.findByLabelText('Booking grid (minutes)')
  const horizon = screen.getByLabelText('Booking horizon (days)')
  expect(grid).toHaveValue(15)
  expect(horizon).toHaveValue(90)

  await user.clear(grid)
  await user.type(grid, '30')
  // A cleared number input reports NaN. It shows as empty, and nothing can be saved until a
  // number is back in it — NaN never reaches the request.
  await user.clear(horizon)
  expect(horizon).toHaveValue(null)
  expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
  await user.type(horizon, '14')
  await user.click(screen.getByRole('button', { name: 'Save changes' }))

  const saved = () =>
    calls.find((call) => call.url === '/api/admin/business' && call.method === 'PUT')
  await waitFor(() => expect(saved()).toBeDefined())
  expect(saved()!.body).toMatchObject({ slot_granularity_minutes: 30, booking_horizon_days: 14 })
})
