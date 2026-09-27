// The production build injects only public, hashed application assets. Never cache API data.
const ASSETS = []
const CACHE = 'linsuite-form-shell-build'

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)))
})
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then(async (names) => {
      await Promise.all(
        names
          .filter(
            (name) => name.startsWith('linsuite-form-shell-') && name !== CACHE,
          )
          .map((name) => caches.delete(name)),
      )
      await self.clients.claim()
    }),
  )
})
self.addEventListener('fetch', (event) => {
  const request = event.request
  const url = new URL(request.url)
  if (request.method !== 'GET' || url.origin !== self.location.origin) return
  const navigation =
    request.mode === 'navigate' &&
    (url.pathname === '/f/' || url.pathname === '/f')
  if (!navigation && !ASSETS.includes(url.pathname)) return
  event.respondWith(
    caches.open(CACHE).then(async (cache) => {
      // Public build assets are identical across Origin headers. The static server's
      // Vary: Origin must not make a crossorigin module miss its precached response.
      return (
        (await cache.match(navigation ? '/f/' : url.pathname, {
          ignoreVary: true,
        })) || fetch(request)
      )
    }),
  )
})
