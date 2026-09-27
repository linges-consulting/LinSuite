import 'fake-indexeddb/auto'
import { screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, test, expect, vi } from 'vitest'
import { renderApp, stubApi, BUSINESS_NAME } from './harness'
import type { PublicForm } from '@/lib/api'

const TOKEN = 'o'.repeat(43)
const KEY = '11111111-1111-4111-8111-111111111111'
const FORM: PublicForm = {
  version_id: 'pinned-v1', template_name: 'Intake',
  schema: { fields: [{ key: KEY, type: 'short_text', label: 'Health history', required: true }] },
  business: { name: BUSINESS_NAME, logo_url: null }, client_first_name: 'Priya',
  expires_at: '2099-09-24T12:00:00Z',
}

afterEach(async () => {
  cleanup()
  sessionStorage.clear()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  // Delete only this test's database after all connections used by the cache have closed.
  await new Promise<void>((resolve, reject) => {
    const request = indexedDB.deleteDatabase('linsuite-forms')
    request.onsuccess = () => resolve()
    request.onerror = () => reject(request.error)
  })
})

function open(respond?: (url: string) => Response | undefined) {
  stubApi({ signedIn: false, respond: (url) =>
    respond?.(url) ?? (url === '/api/public/forms/lookup' ? Response.json(FORM) : undefined),
  })
  window.history.replaceState(null, '', `/f/#${TOKEN}`)
  return renderApp(`/f/#${TOKEN}`)
}

test('a reload without a connection restores the pinned form and the typed answers', async () => {
  const first = open()
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText(/Health history/), 'Previous shoulder injury')
  await new Promise((resolve) => setTimeout(resolve, 650))
  first.unmount()
  const online = vi.mocked(fetch).getMockImplementation()!
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (url === '/api/public/forms/lookup') throw new TypeError('offline')
    return online(url, init)
  })
  renderApp('/f/')
  expect(await screen.findByLabelText(/Health history/)).toHaveValue('Previous shoulder injury')
})

test('a network failure queues a durable submission and online retries the same pinned payload once', async () => {
  open()
  const requests: Record<string, unknown>[] = []
  let connected = false
  const passthrough = vi.mocked(fetch).getMockImplementation()!
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (url !== '/api/public/forms/submit') return passthrough(url, init)
    requests.push(JSON.parse(String(init!.body)))
    if (!connected) throw new TypeError('offline')
    return Response.json({ status: 'already_received' })
  })
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText(/Health history/), 'Shoulder injury')
  await user.click(screen.getByRole('button', { name: 'Submit' }))
  expect(await screen.findByRole('status')).toHaveTextContent('Saved on this device')
  connected = true
  fireEvent(window, new Event('online'))
  fireEvent(window, new Event('online'))
  expect(await screen.findByRole('heading', { name: 'Thank you' })).toBeInTheDocument()
  expect(requests).toHaveLength(2)
  expect(requests[1]).toEqual(requests[0])
  expect(requests[1].version_id).toBe('pinned-v1')
  const { readCachedForm } = await import('@/lib/form-cache')
  await waitFor(async () => expect(await readCachedForm(TOKEN)).toBeUndefined())
  expect(sessionStorage.getItem('linsuite:active-form')).toBeNull()
})

test('a rejected pinned version erases the queued answers and asks staff for a new link', async () => {
  open()
  let connected = false
  const passthrough = vi.mocked(fetch).getMockImplementation()!
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (url !== '/api/public/forms/submit') return passthrough(url, init)
    if (!connected) throw new TypeError('offline')
    return Response.json({ code: 'version_mismatch', detail: 'Changed' }, { status: 409 })
  })
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText(/Health history/), 'Shoulder injury')
  await user.click(screen.getByRole('button', { name: 'Submit' }))
  await screen.findByRole('status')
  connected = true
  fireEvent(window, new Event('online'))
  expect(await screen.findByText(/Please ask staff for a new link/)).toBeInTheDocument()
  const { readCachedForm } = await import('@/lib/form-cache')
  expect(await readCachedForm(TOKEN)).toBeUndefined()
  expect(document.body).not.toHaveTextContent('Shoulder injury')
})

test('an expired draft is deleted when the page loads and cannot be recovered offline', async () => {
  const { saveCachedForm, readCachedForm } = await import('@/lib/form-cache')
  await saveCachedForm({ token: TOKEN, form: { ...FORM, expires_at: '2001-01-01T00:00:00Z' },
    answers: { [KEY]: 'Private expired answer' }, submissionId: crypto.randomUUID(), queued: false })
  sessionStorage.setItem('linsuite:active-form', TOKEN)
  stubApi({ signedIn: false })
  const online = vi.mocked(fetch).getMockImplementation()!
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (url === '/api/public/forms/lookup') throw new TypeError('offline')
    return online(url, init)
  })
  renderApp('/f/')
  expect(await screen.findByRole('heading', { name: 'This form could not be opened' })).toBeInTheDocument()
  expect(await readCachedForm(TOKEN)).toBeUndefined()
  expect(document.body).not.toHaveTextContent('Private expired answer')
})

test('a browser that blocks session storage can still open and submit its form', async () => {
  for (const method of ['getItem', 'setItem', 'removeItem'] as const) {
    const original = Storage.prototype[method]
    vi.spyOn(Storage.prototype, method).mockImplementation(function (this: Storage, ...args: Parameters<typeof original>) {
      if (this === sessionStorage) throw new DOMException('Storage blocked', 'SecurityError')
      return Reflect.apply(original, this, args)
    })
  }
  open((url) => url === '/api/public/forms/submit' ? Response.json({ status: 'received' }) : undefined)
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText(/Health history/), 'Shoulder injury')
  await user.click(screen.getByRole('button', { name: 'Submit' }))
  expect(await screen.findByRole('heading', { name: 'Thank you' })).toBeInTheDocument()
})

test('a queued form survives reload and retries a consumed link without looking it up again', async () => {
  const first = open()
  let connected = false
  const bodies: Record<string, unknown>[] = []
  const passthrough = vi.mocked(fetch).getMockImplementation()!
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (url === '/api/public/forms/submit') {
      bodies.push(JSON.parse(String(init!.body)))
      if (!connected) throw new TypeError('acknowledgement lost')
      return Response.json({ status: 'already_received' })
    }
    if (url === '/api/public/forms/lookup' && connected)
      return Response.json({ code: 'link_invalid' }, { status: 404 })
    return passthrough(url, init)
  })
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText(/Health history/), 'Shoulder injury')
  await user.click(screen.getByRole('button', { name: 'Submit' }))
  await screen.findByRole('status')
  first.unmount()
  connected = true
  renderApp('/f/')
  await screen.findByRole('status')
  fireEvent(window, new Event('online'))
  expect(await screen.findByRole('heading', { name: 'Thank you' })).toBeInTheDocument()
  expect(bodies).toHaveLength(2)
  expect(bodies[1]).toEqual(bodies[0])
  expect(vi.mocked(fetch).mock.calls.filter(([url]) => url === '/api/public/forms/lookup')).toHaveLength(1)
})
