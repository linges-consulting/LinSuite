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
# The account exists and has never had a password: it was created by an administrator and the
# invitation has not been accepted. Its own code, because "incorrect email or password" would
# send somebody hunting for a password nobody ever gave them.
PASSWORD_NOT_SET = "password_not_set"
# The staff member has been deactivated. The credential was right; the account is closed.
ACCOUNT_INACTIVE = "account_inactive"
# A code was wrong, spent or expired — whichever it was, the answer is the same one.
INVALID_MFA_CODE = "invalid_mfa_code"
# The session is real but owes a second factor: route to the verify screen, not to /login.
MFA_VERIFICATION_REQUIRED = "mfa_verification_required"
# The business requires a second factor on accounts that can administer, and this one has
# none: route to enrolment and nowhere else.
MFA_ENROLMENT_REQUIRED = "mfa_enrolment_required"
# Entering Admin Mode needs a code this time round (once per `ADMIN_MFA_INTERVAL_HOURS`).
# The re-authentication dialog grows a second field; nothing about the session changed.
MFA_REQUIRED = "mfa_required"
# An emailed code was asked for where this account may not use one.
MFA_EMAIL_OTP_NOT_ALLOWED = "mfa_email_otp_not_allowed"
# A multipart upload arrived without an `Origin` (or `Referer`) naming this deployment. The
# two upload paths are the only ones exempt from the JSON-only rule, so this stands in for it
# there. Emitted by the middleware in `main.py` rather than raised as `Forbidden`: that handler
# runs inside `ExceptionMiddleware`, which a middleware's own exception never reaches.
UPLOAD_ORIGIN_REQUIRED = "upload_origin_required"
# Redis is unreachable on a path that must not guess: the `jti` denylist, the Admin Mode
# window, this session's MFA state and the credential throttle all fail closed. 503 rather
# than 500 so a screen can say "try again shortly" instead of "something is broken"
# (tech-stack §14). The availability cache is the one caller that degrades instead — a miss
# there is a slower answer, not a weaker one.
SERVICE_UNAVAILABLE = "service_unavailable"


class Forbidden(HTTPException):
    """A 403 that says which kind it is. `main.py` registers the handler that emits `code`."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(status_code=403, detail=detail)
        self.code = code
