import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ApiError, type User } from '@/lib/api'
import { SESSION } from '@/lib/auth'
import { APPOINTMENTS, AVAILABILITY, CUSTOMER_PROFILE, SCHEDULE } from '@/lib/query-keys'

/**
 * One client, with the answers that mean "what you believe about this session is no longer
 * true" handled once rather than at every call site.
 *
 * **401 — the session is gone.** The cookie expired, or somebody revoked it. Nothing this
 * tab holds is usable, so the cached session becomes null and routing takes the browser to
 * the login screen. This is why a refused Admin Mode re-authentication answers 403: it must
 * not look like this.
 *
 * **403 — three different things, told apart by `code` and not by prose.** Before roles
 * existed, every 403 was assumed to be a lapsed admin window, which was true only because
 * there was nothing else it could be. Now a capability refusal and a forced password change
 * arrive the same way, and guessing from the status alone would tell somebody their Admin
 * Mode expired when what actually happened is that their role does not allow the thing.
 *
 * Matching on `detail` was never an option: it is a sentence written for a person, and it
 * gets reworded. `code` is the server's promise about what kind of refusal this is.
 */
export function createQueryClient(): QueryClient {
  const onError = (error: unknown) => {
    if (!(error instanceof ApiError)) return
    if (error.status === 401) {
      client.setQueryData(SESSION, null)
      client.removeQueries({ predicate: (query) => query.queryKey[0] !== SESSION[0] })
      return
    }
    if (error.status !== 403) return

    switch (error.code) {
      case 'admin_mode_required':
        // Only worth saying when this tab still believes it is elevated. The same code comes
        // back for a user who never had the capability, and "Admin Mode expired" would be a
        // lie to somebody who was never in it.
        if (client.getQueryData<User | null>(SESSION)?.mode === 'admin') {
          toast.warning('Admin Mode expired', {
            description: 'Enter your password again to continue administering.',
          })
        }
        // Re-read either way, so the switcher drops back to Staff Mode on its own.
        client.invalidateQueries({ queryKey: SESSION })
        return
      case 'capability_required':
        toast.error("You don't have permission to do that", {
          description: 'Ask an administrator if you need this.',
        })
        return
      case 'password_change_required':
      case 'mfa_verification_required':
      case 'mfa_enrolment_required':
        // No router in here, and none needed: `/me` answers the flag, and the gate in
        // `App.tsx` is what redirects. One mechanism per gate rather than two that can
        // disagree about which screen is showing.
        //
        // No toast either. All three are states the session is *in*, not things that just
        // went wrong — the screen the browser lands on says what is owed, and a toast
        // beside it would be the same sentence twice.
        client.invalidateQueries({ queryKey: SESSION })
        return
      case 'mfa_required':
        // Entering Admin Mode needs a code this time round. Nothing about the session
        // changed and nothing global should react: the re-authentication dialog asked, and
        // the dialog is what grows a second field.
        return
    }
  }

  const client = new QueryClient({
    queryCache: new QueryCache({ onError }),
    mutationCache: new MutationCache({ onError }),
    defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
  })
  return client
}

/**
 * An input the availability engine reads has changed — a shift, an absence, a closure, a
 * service, a resource, a staff member. The calendar and the slot lists are computed from
 * those, so an administrative edit that invalidates only its own list leaves a booking
 * screen offering times the server will now refuse (or hiding times it would now accept)
 * until the query goes stale on its own.
 *
 * Appointment changes on the calendar call it too, so it also refreshes client profiles.
 *
 * The server bumps its own cache generation on the same writes (`scheduling/cache.py`);
 * this is the browser's half of the same job.
 */
export function invalidateScheduling(client: QueryClient): void {
  client.invalidateQueries({ queryKey: SCHEDULE })
  client.invalidateQueries({ queryKey: APPOINTMENTS })
  client.invalidateQueries({ queryKey: AVAILABILITY })
  // Every profile's visit list, which the erasure dialog counts upcoming visits from.
  client.invalidateQueries({ queryKey: CUSTOMER_PROFILE })
}
