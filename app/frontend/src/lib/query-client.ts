import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ApiError, type User } from '@/lib/api'
import { SESSION } from '@/lib/auth'

/**
 * One client, with the two answers that mean "what you believe about this session is no
 * longer true" handled once rather than at every call site.
 *
 * **401 — the session is gone.** The cookie expired, or somebody revoked it. Nothing this
 * tab holds is usable, so the cached session becomes null and routing takes the browser to
 * the login screen. This is why a refused Admin Mode re-authentication answers 403: it must
 * not look like this.
 *
 * **403 while the UI believes it is in Admin Mode — the window lapsed.** The staff session
 * is untouched; only the elevation is gone. Say so in a toast, because an action that
 * silently did nothing is worse than one that explains itself, and re-read `/me` so the
 * switcher drops back to Staff Mode on its own.
 */
export function createQueryClient(): QueryClient {
  const onError = (error: unknown) => {
    if (!(error instanceof ApiError)) return
    if (error.status === 401) {
      client.setQueryData(SESSION, null)
      client.removeQueries({ predicate: (query) => query.queryKey[0] !== SESSION[0] })
      return
    }
    if (error.status === 403 && client.getQueryData<User | null>(SESSION)?.mode === 'admin') {
      toast.warning('Admin Mode expired', {
        description: 'Enter your password again to continue administering.',
      })
      client.invalidateQueries({ queryKey: SESSION })
    }
  }

  const client = new QueryClient({
    queryCache: new QueryCache({ onError }),
    mutationCache: new MutationCache({ onError }),
    defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
  })
  return client
}
