import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { changePassword, fetchMe, login, logout, switchMode, type User } from '@/lib/api'

/**
 * The session, as server state.
 *
 * There is no Context provider here on purpose: the token lives in an httpOnly cookie that
 * JavaScript cannot read, so the only source of truth is `/api/auth/me`. TanStack Query's
 * cache already gives every component one shared, deduplicated answer — a Context around it
 * would be a second copy of the same thing, with its own staleness.
 */
export const SESSION = ['session'] as const

export function useSession() {
  const { data, isPending } = useQuery({
    queryKey: SESSION,
    queryFn: fetchMe,
    // Not `staleTime: Infinity`. Two things end a session without this tab hearing about
    // it — the cookie expiring and an administrator revoking it — and the admin window ends
    // on a schedule of its own. Asking again on focus and on a timer is the only way a page
    // that cannot read the cookie finds out. In Admin Mode the timer tightens, because the
    // countdown drawn from this answer has to stay honest to the second.
    staleTime: 10_000,
    refetchOnWindowFocus: true,
    refetchInterval: (query) => (query.state.data?.mode === 'admin' ? 5_000 : 30_000),
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

export function useSwitchMode() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: switchMode,
    // The answer is the same shape `/me` returns, so it replaces the session outright —
    // mode, countdown and all — without a round trip to confirm what we were just told.
    onSuccess: (user: User) => {
      queryClient.setQueryData(SESSION, user)
      // Changing mode changes what the server will answer. Anything fetched under the old
      // mode is now the wrong answer — a screen that was refused in Staff Mode would go on
      // showing that refusal after the window opened, which reads as the feature being
      // broken rather than as data that was never re-asked for.
      queryClient.invalidateQueries({ predicate: (query) => query.queryKey[0] !== SESSION[0] })
    },
  })
}

export function useChangePassword() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: changePassword,
    // The response replaced the session cookie, and its body is the same shape `/me`
    // returns — including `must_change_password: false`, which is what routes out of the
    // forced-change screen without a round trip to confirm what we were just told. The
    // toast lives here rather than in the screen because the screen unmounts on success.
    onSuccess: (user: User) => {
      queryClient.setQueryData(SESSION, user)
      toast.success('Password changed', { description: 'Every other device has been signed out.' })
    },
  })
}
