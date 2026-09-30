import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { applyBranding, applyCachedBranding } from '@/lib/branding'
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
  // The `<link rel="icon">` is created in `<head>`, which jsdom keeps across tests in a file.
  document.querySelector('link[rel="icon"]')?.remove()
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

test('a wordmark logo keeps its aspect ratio instead of being squeezed into a square', async () => {
  stubApi({ signedIn: true })

  renderApp('/')

  const logo = await screen.findByRole('presentation', { hidden: true })
  // Height is pinned to the sidebar row; the width follows. `size-6` on both axes would
  // render an ordinary wide wordmark at about 24x12 and make it unreadable.
  expect(logo).toHaveClass('h-6', 'w-auto', 'object-contain')
  expect(logo.className).not.toMatch(/\bsize-6\b/)
})

test('removing the favicon puts the stock mark back without a reload', async () => {
  // The tab as it stands before the administrator removes the icon.
  applyBranding(BRANDING_DOCUMENT)
  const icon = document.querySelector<HTMLLinkElement>('link[rel="icon"]')!
  expect(icon.href).toContain('/api/branding/favicon')

  stubApi({
    signedIn: true,
    brandingDocument: { ...BRANDING_DOCUMENT, favicon_url: null, favicon_etag: null },
  })
  renderApp('/')

  await waitFor(() => expect(icon.href).toContain('/favicon.svg'))
})

test('a cache this version cannot read is dropped rather than re-applied every load', async () => {
  localStorage.setItem('branding', 'not json')

  applyCachedBranding()

  expect(localStorage.getItem('branding')).toBeNull()
})

// --- the Branding panel ---------------------------------------------------------------------

async function openBranding() {
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Branding' }))
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

test('typing a colour asks the server once, not once per keystroke', async () => {
  const { calls } = stubApi({ signedIn: true, dualRole: true, adminWindowMs: 15 * 60_000 })
  renderApp('/settings')
  const user = await openBranding()

  const primary = await screen.findByRole('textbox', { name: 'Primary' })
  await user.clear(primary)
  await user.type(primary, '#b91c1c')

  // The settled value is asked for; the six intermediate shades are not — each would be a
  // round trip *and* a cache entry that lives for the rest of the session.
  const previews = () => calls.filter((c) => c.url.includes('/branding/preview'))
  await waitFor(() => expect(previews()[0]?.url).toContain('%23b91c1c'))
  expect(previews()).toHaveLength(1)
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
