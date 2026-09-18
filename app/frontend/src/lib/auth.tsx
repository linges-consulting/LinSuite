import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchMe, login, logout, type User } from '@/lib/api'

/**
 * The session, as server state.
 *
 * There is no Context provider here on purpose: the token lives in an httpOnly cookie that
 * JavaScript cannot read, so the only source of truth is `/api/auth/me`. TanStack Query's
 * cache already gives every component one shared, deduplicated answer — a Context around it
 * would be a second copy of the same thing, with its own staleness.
 */
const SESSION = ['session'] as const

export function useSession() {
  const { data, isPending } = useQuery({
    queryKey: SESSION,
    queryFn: fetchMe,
    staleTime: Infinity,
    retry: false,
  })
  return { user: data ?? null, isPending }
}

export function useLogin() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: login,
    onSuccess: (user: User) => queryClient.setQueryData(SESSION, user),
  })
}

export function useLogout() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: logout,
    // Everything else in the cache was fetched for the person who just left, so it goes.
    // The session is set to null rather than dropped: `queryClient.clear()` would discard
    // the query this hook's observers are subscribed to, and they would keep rendering the
    // old user until something else re-rendered them.
    onSettled: () => {
      queryClient.setQueryData(SESSION, null)
      queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== SESSION[0] })
    },
  })
}
