/** Matches `MIN_PASSWORD_LENGTH` in core/security.py. The server is the authority — this is
 *  only so a twelve-character rule is not learned from a round trip. */
export const MIN_PASSWORD_LENGTH = 12

/**
 * The one client-side check the reset and change screens run before spending a request.
 *
 * The confirmation is never sent: the server has no business knowing whether a user typed
 * the same thing twice. It exists because neither screen can show what was typed and both
 * end in a credential a typo makes unrecoverable.
 */
export function localPasswordProblem(password: string, confirm: string) {
  if (password.length < MIN_PASSWORD_LENGTH)
    return { error: `Use at least ${MIN_PASSWORD_LENGTH} characters.` }
  if (password !== confirm) return { confirmError: 'The two passwords do not match.' }
  return null
}
