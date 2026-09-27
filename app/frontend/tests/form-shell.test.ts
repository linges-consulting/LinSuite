/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { expect, test, vi } from 'vitest'

function tabletWorker(varyOrigin = false) {
  const listeners = new Map<string, (event: Record<string, unknown>) => void>()
  const stored = new Map<string, Response>([
    ['/f/', new Response('<html>cached tablet shell</html>')],
    ['/assets/form.js', new Response('cached application code')],
  ])
  const network = vi.fn(() => Promise.reject(new TypeError('offline')))
  const cache = {
    match: async (
      key: string | Request,
      options?: { ignoreVary?: boolean },
    ) => {
      const name = typeof key === 'string' ? key : new URL(key.url).pathname
      // The precache has no Origin header; a crossorigin module request does. Vite emits
      // Vary: Origin even for its public static assets, so normal Cache matching misses it.
      if (varyOrigin && name.endsWith('.js') && !options?.ignoreVary)
        return undefined
      return stored.get(name)
    },
  }
  const source = readFileSync('build/form-offline-worker.js', 'utf8').replace(
    'const ASSETS = []',
    'const ASSETS = ["/f/", "/assets/form.js"]',
  )
  runInNewContext(source, {
    self: {
      location: { origin: 'https://clinic.test' },
      addEventListener: (
        name: string,
        handler: (event: Record<string, unknown>) => void,
      ) => listeners.set(name, handler),
    },
    caches: { open: async () => cache },
    fetch: network,
    URL,
    Response,
  })
  const load = (url: string, mode = 'cors', method = 'GET') => {
    let result: Promise<Response> | undefined
    listeners.get('fetch')!({
      request: { url, mode, method },
      respondWith: (response: Promise<Response>) => {
        result = response
      },
    })
    return result
  }
  return { load, network }
}

test('a disconnected tablet reload receives both the cached shell and application code', async () => {
  const tablet = tabletWorker()
  expect(
    await (await tablet.load('https://clinic.test/f/', 'navigate'))!.text(),
  ).toContain('cached tablet shell')
  expect(
    await (await tablet.load('https://clinic.test/assets/form.js'))!.text(),
  ).toBe('cached application code')
  expect(tablet.network).not.toHaveBeenCalled()
})

test('the tablet shell worker never intercepts client data, submissions, other pages or external assets', () => {
  const tablet = tabletWorker()
  for (const [url, mode, method] of [
    ['https://clinic.test/api/customers/c1', 'cors', 'GET'],
    ['https://clinic.test/api/public/forms/submit', 'cors', 'POST'],
    ['https://clinic.test/clients/c1', 'navigate', 'GET'],
    ['https://other.test/assets/form.js', 'cors', 'GET'],
  ])
    expect(tablet.load(url, mode, method)).toBeUndefined()
  expect(tablet.network).not.toHaveBeenCalled()
})

test('a public module precached with Vary Origin still loads during a disconnected reload', async () => {
  const tablet = tabletWorker(true)
  expect(
    await (await tablet.load('https://clinic.test/assets/form.js'))!.text(),
  ).toBe('cached application code')
})
