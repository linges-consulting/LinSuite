import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { renderApp, stubApi, type Call } from './harness'

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

/**
 * The retention profile on Settings → Security (ADR-0001, pre-flight D3). What is pinned: a
 * banner asks for a choice until one has been saved, both options explain which obligation
 * wins, releasing holds asks first and says so, and saving the MFA switches never quietly
 * "chooses" the profile on the administrator's behalf.
 */

const ADMIN = { signedIn: true, dualRole: true, adminWindowMs: 15 * 60_000 }
const SECURITY = '/api/admin/business/security'

type Policy = {
  mfa_required_for_admin: boolean
  mfa_email_otp_allowed: boolean
  retention_profile: string
  retention_profile_chosen: boolean
}

function fakeSecurity(
  initial: { retention_profile: string; retention_profile_chosen: boolean },
  // Somebody else's save, landing between this screen's reads.
  elsewhere?: (policy: Policy) => Policy,
) {
  let policy: Policy = { mfa_required_for_admin: true, mfa_email_otp_allowed: false, ...initial }
  return stubApi({
    ...ADMIN,
    respond: (url, body) => {
      if (url !== SECURITY) return undefined
      if (elsewhere) policy = elsewhere(policy)
      if (body !== undefined) {
        policy = {
          ...policy,
          ...body,
          retention_profile_chosen:
            policy.retention_profile_chosen || body.retention_profile !== undefined,
        }
      }
      return Response.json(policy)
    },
  })
}

const patches = (calls: Call[]) => calls.filter((c) => c.url === SECURITY && c.method === 'PATCH')

async function openSecurity() {
  const user = userEvent.setup()
  renderApp('/settings')
  await user.click(await screen.findByRole('tab', { name: 'Security' }))
  return user
}

test('a banner asks for a choice until the profile has been saved, and both options explain themselves', async () => {
  const { calls } = fakeSecurity({ retention_profile: 'regulated_health', retention_profile_chosen: false })
  const user = await openSecurity()

  expect(await screen.findByText(/Choose a retention profile/)).toBeInTheDocument()
  const regulated = screen.getByRole('radio', { name: /Regulated health practice/ })
  const general = screen.getByRole('radio', { name: /General business/ })
  expect(regulated).toBeChecked()
  expect(general).not.toBeChecked()
  expect(screen.getByText(/retention obligation wins/)).toBeInTheDocument()
  expect(screen.getByText(/deletion request is honoured promptly/)).toBeInTheDocument()
  expect(screen.getByText(/college or counsel/)).toBeInTheDocument()

  // Saving the default is still a decision: it is sent, and the banner goes.
  await user.click(screen.getByRole('button', { name: 'Save retention profile' }))

  await waitFor(() => expect(patches(calls)).toHaveLength(1))
  expect(patches(calls)[0].body).toEqual({ retention_profile: 'regulated_health' })
  await waitFor(() => expect(screen.queryByText(/Choose a retention profile/)).not.toBeInTheDocument())
})

test('no banner once a profile has been chosen', async () => {
  fakeSecurity({ retention_profile: 'general_business', retention_profile_chosen: true })
  await openSecurity()

  expect(await screen.findByRole('radio', { name: /General business/ })).toBeChecked()
  expect(screen.queryByText(/Choose a retention profile/)).not.toBeInTheDocument()
})

test('switching to general business asks first and says holds are released', async () => {
  const { calls } = fakeSecurity({ retention_profile: 'regulated_health', retention_profile_chosen: true })
  const user = await openSecurity()

  await user.click(await screen.findByRole('radio', { name: /General business/ }))
  await user.click(screen.getByRole('button', { name: 'Save retention profile' }))

  const dialog = await screen.findByRole('dialog')
  expect(dialog).toHaveTextContent(/no longer be held/)
  expect(patches(calls)).toHaveLength(0)

  await user.click(within(dialog).getByRole('button', { name: 'Release retention holds' }))

  await waitFor(() => expect(patches(calls)).toHaveLength(1))
  expect(patches(calls)[0].body).toEqual({ retention_profile: 'general_business' })
})

test('cancelling the confirmation sends nothing', async () => {
  const { calls } = fakeSecurity({ retention_profile: 'regulated_health', retention_profile_chosen: true })
  const user = await openSecurity()

  await user.click(await screen.findByRole('radio', { name: /General business/ }))
  await user.click(screen.getByRole('button', { name: 'Save retention profile' }))
  await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Cancel' }))

  expect(patches(calls)).toHaveLength(0)
})

test('flipping an MFA switch sends only the MFA switches', async () => {
  const { calls } = fakeSecurity({ retention_profile: 'regulated_health', retention_profile_chosen: false })
  const user = await openSecurity()

  await user.click(await screen.findByRole('checkbox', { name: /Allow emailed codes/ }))

  await waitFor(() => expect(patches(calls)).toHaveLength(1))
  expect(patches(calls)[0].body).toEqual({
    mfa_required_for_admin: true,
    mfa_email_otp_allowed: true,
  })
  expect(screen.getByText(/Choose a retention profile/)).toBeInTheDocument()
})

test('the radio follows a profile somebody else saved when nothing is being edited', async () => {
  let reads = 0
  fakeSecurity({ retention_profile: 'regulated_health', retention_profile_chosen: true }, (p) =>
    ++reads > 1 ? { ...p, retention_profile: 'general_business' } : p,
  )
  const user = await openSecurity()
  expect(await screen.findByRole('radio', { name: /Regulated health practice/ })).toBeChecked()

  // Any fresh read — here, the answer to an MFA save — carries the other administrator's change.
  await user.click(screen.getByRole('checkbox', { name: /Allow emailed codes/ }))

  await waitFor(() => expect(screen.getByRole('radio', { name: /General business/ })).toBeChecked())
})

test('a fresh read never clobbers a choice being edited', async () => {
  let reads = 0
  fakeSecurity({ retention_profile: 'regulated_health', retention_profile_chosen: true }, (p) =>
    // A visible sign the fresh read landed: the server now says nothing is chosen.
    ++reads > 1 ? { ...p, retention_profile_chosen: false } : p,
  )
  const user = await openSecurity()
  await user.click(await screen.findByRole('radio', { name: /General business/ }))

  await user.click(screen.getByRole('checkbox', { name: /Require two-factor/ }))
  expect(await screen.findByText(/Choose a retention profile/)).toBeInTheDocument()

  expect(screen.getByRole('radio', { name: /General business/ })).toBeChecked()
})
