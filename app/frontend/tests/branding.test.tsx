import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {
  BRANDING_DOCUMENT,
  BUSINESS_NAME,
  LOW_CONTRAST,
  PRIMARY,
  PRIMARY_DARK,
  renderApp,
  stubApi,
} from './harness'

afterEach(() => {
  vi.unstubAllGlobals()
  document.documentElement.removeAttribute('style')
  localStorage.clear()
})

/**
 * White-label is one set of CSS variables on `<html>`, so these tests assert on the variables
 * and not on rendered colour — the whole point of `docs/DESIGN.md`'s layer 1 is that no
 * component has to know a business changed its brand.
 */

test('the business brand is applied at boot, before anybody signs in', async () => {
  stubApi({ signedIn: false })

  renderApp('/login')

  await waitFor(() =>
    expect(document.documentElement.style.getPropertyValue('--brand-primary')).toBe(PRIMARY),
  )
  // The dark-theme variant comes from the server beside the colour it was derived from.
  expect(document.documentElement.style.getPropertyValue('--brand-primary-dark')).toBe(
    PRIMARY_DARK,
  )
  expect(document.title).toBe(BUSINESS_NAME)
  expect(document.querySelector<HTMLLinkElement>('link[rel="icon"]')?.href).toContain(
    '/api/branding/favicon',
  )
})

test('the business name and logo replace the product name in the shell', async () => {
  stubApi({ signedIn: true })

  renderApp('/')

  expect(await screen.findByText(BUSINESS_NAME)).toBeInTheDocument()
  await waitFor(() =>
    expect(document.querySelector('img[src^="/api/branding/logo"]')).toBeInTheDocument(),
  )
})

test('an instance with no logo shows the brand mark rather than a broken image', async () => {
  stubApi({
    signedIn: true,
    brandingDocument: { ...BRANDING_DOCUMENT, logo_url: null, logo_etag: null },
  })

  renderApp('/')

  expect(await screen.findByText(BUSINESS_NAME)).toBeInTheDocument()
  await waitFor(() => expect(document.querySelector('img[src^="/api/branding"]')).toBeNull())
})

// --- the Branding panel ---------------------------------------------------------------------

async function openBranding() {
  const user = userEvent.setup()
  await user.click(await screen.findByRole('tab', { name: 'Branding' }))
  return user
}

test('a colour whose text falls below 4.5:1 warns, and is still allowed', async () => {
  stubApi({ signedIn: true, dualRole: true, adminWindowMs: 15 * 60_000 })
  renderApp('/settings')
  const user = await openBranding()

  // Comfortable to start with: the fake reports 8.6:1 for anything but the one colour.
  expect(await screen.findAllByText(/8\.6:1 light/)).toHaveLength(2)

  // By role, not by label text: the sidebar's `<nav aria-label="Primary">` is also a
  // "Primary" in this document.
  const primary = await screen.findByRole('textbox', { name: 'Primary' })
  await user.clear(primary)
  await user.type(primary, LOW_CONTRAST)

  const warning = await screen.findByRole('alert')
  expect(warning).toHaveTextContent(/3\.1:1, below the 4\.5:1 readability guideline/)
  // A warning, never a refusal: it is the business's own brand.
  expect(screen.getByRole('button', { name: 'Save colours' })).toBeEnabled()
})

test('a malformed hex is refused before it can be saved', async () => {
  stubApi({ signedIn: true, dualRole: true, adminWindowMs: 15 * 60_000 })
  renderApp('/settings')
  const user = await openBranding()

  const primary = await screen.findByRole('textbox', { name: 'Primary' })
  await user.clear(primary)
  await user.type(primary, '#nope')

  expect(await screen.findByText(/six-digit hex colour/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Save colours' })).toBeDisabled()
})

test('the preview shows the colour on both themes', async () => {
  stubApi({ signedIn: true, dualRole: true, adminWindowMs: 15 * 60_000 })
  renderApp('/settings')
  await openBranding()

  const dark = await screen.findByTestId('preview-dark')
  const light = await screen.findByTestId('preview-light')
  // `.dark` resolves `--primary` from `--brand-primary-dark`; `.light` from `--brand-primary`.
  expect(dark).toHaveClass('dark')
  expect(dark.style.getPropertyValue('--brand-primary-dark')).toBe(PRIMARY_DARK)
  expect(light.style.getPropertyValue('--brand-primary')).toBe(PRIMARY)
  expect(within(light).getByRole('button', { name: 'Book' })).toBeInTheDocument()
})
