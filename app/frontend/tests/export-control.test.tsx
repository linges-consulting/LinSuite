import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { ExportControl } from '@/components/export-control'
import { downloadAccessLogExport, fetchAccessLogExportStatus, requestAccessLogExport } from '@/lib/api'

/**
 * The reusable export control (#86/#95's shared mechanism, #98): idle → request → preparing
 * (polled until ready/failed) → download, with a 410 on download read as "expired". Exercised
 * here against the client access-log export endpoints (#87/#92) — the same three calls
 * `commission_report.py` and `package_liability.py` answer, so this is also the contract a
 * later ticket wires those two reports against.
 */

const EXPORTS_PATH = '/api/admin/customers/c1/access-log/exports'

function job(status: string) {
  return {
    id: 'e1',
    status,
    created_at: '2026-09-19T12:00:00Z',
    completed_at: status === 'pending' ? null : '2026-09-19T12:00:05Z',
    download_url: status === 'ready' ? `${EXPORTS_PATH}/e1/csv` : null,
  }
}

function stubExportApi({
  pollStatuses = ['ready'],
  downloadStatus,
}: { pollStatuses?: string[]; downloadStatus?: number } = {}) {
  const calls: { url: string; method: string; body?: unknown }[] = []
  let pollIndex = 0
  const fetchMock = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const parsed = new URL(String(url), 'http://test')
    calls.push({
      url: parsed.pathname,
      method: init?.method ?? 'GET',
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined,
    })
    if (parsed.pathname === EXPORTS_PATH && (init?.method ?? 'GET') === 'POST') {
      return Response.json(job('pending'), { status: 202 })
    }
    if (parsed.pathname === `${EXPORTS_PATH}/e1`) {
      const status = pollStatuses[Math.min(pollIndex, pollStatuses.length - 1)]
      pollIndex += 1
      return Response.json(job(status))
    }
    if (parsed.pathname === `${EXPORTS_PATH}/e1/csv`) {
      if (downloadStatus === 410) {
        return Response.json({ detail: 'This export has expired.' }, { status: 410 })
      }
      return new Response(new Blob(['id,when\n1,now'], { type: 'text/csv' }), {
        headers: { 'Content-Type': 'text/csv', 'Content-Disposition': 'attachment; filename="access_log.csv"' },
      })
    }
    throw new Error(`unexpected fetch: ${parsed.pathname}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  return calls
}

function renderControl(range = { from: '2026-06-21', to: '2026-09-19' }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ExportControl
        label="Export CSV"
        requestExport={() => requestAccessLogExport('c1', range)}
        pollExport={(id) => fetchAccessLogExportStatus('c1', id)}
        downloadExport={(id) => downloadAccessLogExport('c1', id)}
      />
    </QueryClientProvider>,
  )
}

afterEach(() => vi.unstubAllGlobals())

test('idle requests an export for the caller-supplied range, polls until ready, then downloads it', async () => {
  const calls = stubExportApi({ pollStatuses: ['pending', 'ready'] })
  const create = vi.fn(() => 'blob:export')
  const revoke = vi.fn()
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke }))
  const user = userEvent.setup()
  renderControl()

  await user.click(screen.getByRole('button', { name: 'Export CSV' }))
  expect(await screen.findByText('Preparing…')).toBeInTheDocument()

  const download = await screen.findByRole('button', { name: 'Download CSV' }, { timeout: 6000 })
  await user.click(download)
  await waitFor(() => expect(create).toHaveBeenCalled())
  expect(revoke).toHaveBeenCalled()

  const posted = calls.find((c) => c.method === 'POST')
  expect(posted?.body).toEqual({ from: '2026-06-21', to: '2026-09-19' })
})

test('a failed export shows an error and offers to try again', async () => {
  stubExportApi({ pollStatuses: ['failed'] })
  const user = userEvent.setup()
  renderControl()

  await user.click(screen.getByRole('button', { name: 'Export CSV' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('The export failed.')
  expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
})

test('a 410 on download shows expired and offers a fresh request', async () => {
  const calls = stubExportApi({ pollStatuses: ['ready'], downloadStatus: 410 })
  const user = userEvent.setup()
  renderControl()

  await user.click(screen.getByRole('button', { name: 'Export CSV' }))
  const download = await screen.findByRole('button', { name: 'Download CSV' })
  await user.click(download)

  expect(await screen.findByRole('alert')).toHaveTextContent('This export has expired.')
  const again = screen.getByRole('button', { name: 'Request a new export' })

  await user.click(again)
  await waitFor(() => expect(calls.filter((c) => c.method === 'POST')).toHaveLength(2))
})
