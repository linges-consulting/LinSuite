"""The one 403 shape the API emits.

Several different things answer 403, and they ask the browser for different responses: the
Admin Mode window has lapsed (re-authenticate), the role lacks a capability (apologise and
stay put), a password change is owed (go to that screen and nowhere else), or a credential
was simply wrong (the screen that asked says so, and nothing global should react). Prose
cannot be switched on — it is written for a person, it gets reworded, and a frontend that
matched on it would break silently the first time somebody improved a sentence.

So every 403 this API emits carries a stable `code` beside its `detail`, and
`test_every_403_the_api_emits_carries_a_code` is what keeps that sentence true. The
alternative — some coded and some not — is what tempts a test fake into inventing a code the
server never sends, which is exactly how "Incorrect password" came to be labelled
`admin_mode_required` in one and would have toasted "Admin Mode expired" at somebody who
mistyped.

`detail` stays a plain string, so the existing client-side error reader is unchanged.
Codes the frontend deliberately does not act on are still worth sending: they are what makes
"did the session state change?" answerable without parsing a sentence.
"""

from fastapi import HTTPException

ADMIN_MODE_REQUIRED = "admin_mode_required"
CAPABILITY_REQUIRED = "capability_required"
PASSWORD_CHANGE_REQUIRED = "password_change_required"
# A credential was wrong. Nothing about the session changed, so `query-client.ts` acts on
# none of these — the form that collected the password is the only thing that should react.
INVALID_PASSWORD = "invalid_password"
INVALID_SETUP_TOKEN = "invalid_setup_token"


class Forbidden(HTTPException):
    """A 403 that says which kind it is. `main.py` registers the handler that emits `code`."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(status_code=403, detail=detail)
        self.code = code
