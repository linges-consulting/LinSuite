import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Toaster } from '@/components/ui/sonner'
import { createQueryClient } from '@/lib/query-client'
import { ThemeProvider } from '@/lib/theme'
import { TaxSettingsPanel } from '@/routes/settings-tax'

/**
 * Settings → Billing → Tax's pre-fill confirmation and prompt (#118, spec #113 "Tax
 * pre-fill"): the "Looks right" banner shown only while a pre-filled component is unconfirmed,
 * and the separate province-changed/newer-rate prompt, which never disappears on its own and
 * never changes anything by itself. `TaxSettingsPanel` takes no props — rendered directly, the
 * way a checklist step page wrapping it (built in parallel) will render it too.
 */

const PROVINCES = [
  { code: 'BC', name: 'British Columbia', timezone: 'America/Vancouver' },
  { code: 'ON', name: 'Ontario', timezone: 'America/Toronto' },
]

const GST = {
  id: 'c1',
  code: 'GST',
  name: 'Goods and Services Tax',
  province: 'BC',
  active: true,
  rates: [{ id: 'r1', rate_ppm: 50_000, effective_from: '2008-01-01', effective_to: null }],
  current_rate_ppm: 50_000,
  applicable_to_business: true,
  origin: 'prefill' as const,
}

type Status = {
  confirmed_at: string | null
  prefilled: boolean
  province_changed: boolean
  newer_rate_available: boolean
}

function fakeServer(status: Status) {
  const calls: { url: string; method: string }[] = []

  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      calls.push({ url, method })

      if (url === '/api/admin/billing/tax-components') {
        return Response.json({ tax_components: status.prefilled ? [GST] : [] })
      }
      if (url === '/api/admin/billing/tax-status') {
        return Response.json(status)
      }
      if (url === '/api/admin/billing/tax-confirmation' && method === 'POST') {
        status.confirmed_at = '2026-09-29T12:00:00Z'
        return Response.json(status)
      }
      if (url === '/api/admin/business/provinces') return Response.json(PROVINCES)

      return Response.json({}, { status: 404 })
    }),
  )
  return { calls }
}

function renderPanel() {
  return render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <TaxSettingsPanel />
        <Toaster position="bottom-right" />
      </QueryClientProvider>
    </ThemeProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('tax pre-fill confirmation', () => {
  it('shows "Looks right" for an unconfirmed pre-filled component', async () => {
    fakeServer({
      confirmed_at: null,
      prefilled: true,
      province_changed: false,
      newer_rate_available: false,
    })

    renderPanel()

    expect(await screen.findByRole('button', { name: 'Looks right' })).toBeInTheDocument()
  })

  it('confirms and clears the banner', async () => {
    const { calls } = fakeServer({
      confirmed_at: null,
      prefilled: true,
      province_changed: false,
      newer_rate_available: false,
    })
    const user = userEvent.setup()
    renderPanel()

    await user.click(await screen.findByRole('button', { name: 'Looks right' }))

    await waitFor(() =>
      expect(calls.some((c) => c.url === '/api/admin/billing/tax-confirmation' && c.method === 'POST')).toBe(
        true,
      ),
    )
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Looks right' })).not.toBeInTheDocument(),
    )
  })

  it('shows no banner once confirmed and nothing has changed', async () => {
    fakeServer({
      confirmed_at: '2026-01-01T00:00:00Z',
      prefilled: true,
      province_changed: false,
      newer_rate_available: false,
    })

    renderPanel()

    await screen.findByText('GST')
    expect(screen.queryByRole('button', { name: 'Looks right' })).not.toBeInTheDocument()
    expect(screen.queryByText(/province has changed/i)).not.toBeInTheDocument()
  })

  it('prompts on a province change, even after confirmation, without editing anything', async () => {
    fakeServer({
      confirmed_at: '2026-01-01T00:00:00Z',
      prefilled: true,
      province_changed: true,
      newer_rate_available: false,
    })

    renderPanel()

    expect(await screen.findByText(/province has changed/i)).toBeInTheDocument()
    // The prompt is informational only — no confirmation button, and the existing component
    // is still listed exactly as it was.
    expect(screen.queryByRole('button', { name: 'Looks right' })).not.toBeInTheDocument()
    expect(await screen.findByText('GST')).toBeInTheDocument()
  })

  it('prompts when the table has a newer rate than the business has', async () => {
    fakeServer({
      confirmed_at: '2026-01-01T00:00:00Z',
      prefilled: true,
      province_changed: false,
      newer_rate_available: true,
    })

    renderPanel()

    expect(await screen.findByText(/newer rate/i)).toBeInTheDocument()
  })

  it('shows nothing when there is no pre-fill at all', async () => {
    fakeServer({
      confirmed_at: null,
      prefilled: false,
      province_changed: false,
      newer_rate_available: false,
    })

    renderPanel()

    await screen.findByText('No tax components yet.')
    expect(screen.queryByRole('button', { name: 'Looks right' })).not.toBeInTheDocument()
  })
})

// --- #119: rate_ppm, not basis points ------------------------------------------------------

const CUSTOM = {
  ...GST,
  id: 'c-custom',
  code: 'LEVY',
  name: 'Local levy',
  rates: [{ id: 'r3', rate_ppm: 12_345, effective_from: '2026-01-01', effective_to: null }],
  current_rate_ppm: 12_345,
  origin: 'manual' as const,
}

const QST = {
  id: 'c2',
  code: 'QST',
  name: 'Quebec Sales Tax',
  province: 'QC',
  active: true,
  rates: [{ id: 'r2', rate_ppm: 99_750, effective_from: '2013-01-01', effective_to: null }],
  current_rate_ppm: 99_750,
  applicable_to_business: false,
  origin: 'manual' as const,
}

describe('tax rate display and input', () => {
  it('shows a whole rate with no trailing zeros and a fractional rate exactly', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/admin/billing/tax-components') {
          return Response.json({ tax_components: [GST, QST, CUSTOM] })
        }
        if (url === '/api/admin/billing/tax-status') {
          return Response.json({
            confirmed_at: '2026-01-01T00:00:00Z',
            prefilled: false,
            province_changed: false,
            newer_rate_available: false,
          })
        }
        if (url === '/api/admin/business/provinces') return Response.json(PROVINCES)
        return Response.json({}, { status: 404 })
      }),
    )

    renderPanel()

    expect(await screen.findByText('5%')).toBeInTheDocument() // not "5.000%"
    expect(await screen.findByText('9.975%')).toBeInTheDocument() // QST, exact
    // Whatever was stored is what shows: a ppm is a ten-thousandth of a percent, so four
    // decimals show any rate exactly — never rounded to three.
    expect(await screen.findByText('1.2345%')).toBeInTheDocument()
  })

  it('accepts 9.975 and round-trips it as exactly 99_750 ppm, never basis points', async () => {
    let created: { rate_ppm: number } | undefined
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        const method = init?.method ?? 'GET'
        if (url === '/api/admin/billing/tax-components' && method === 'GET') {
          return Response.json({ tax_components: created ? [QST] : [] })
        }
        if (url === '/api/admin/billing/tax-components' && method === 'POST') {
          created = JSON.parse(init!.body as string)
          return Response.json({ ...QST, rates: [{ ...QST.rates[0], rate_ppm: created!.rate_ppm }] })
        }
        if (url === '/api/admin/billing/tax-status') {
          return Response.json({
            confirmed_at: null,
            prefilled: false,
            province_changed: false,
            newer_rate_available: false,
          })
        }
        if (url === '/api/admin/business/provinces') return Response.json(PROVINCES)
        return Response.json({}, { status: 404 })
      }),
    )

    const user = userEvent.setup()
    renderPanel()

    await user.click(await screen.findByRole('button', { name: 'Add tax component' }))
    await user.type(screen.getByLabelText('Code'), 'qst')
    await user.type(screen.getByLabelText('Name'), 'QST')
    await user.clear(screen.getByLabelText('Rate'))
    await user.type(screen.getByLabelText('Rate'), '9.975')
    await user.click(screen.getByRole('button', { name: 'Add component' }))

    await waitFor(() => expect(created).toBeDefined())
    expect(created!.rate_ppm).toBe(99_750)
  })
})
