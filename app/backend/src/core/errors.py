"""The one 403 shape the API emits.

Three different things answer 403, and they ask the browser for three different responses:
the Admin Mode window has lapsed (re-authenticate), the role lacks a capability (apologise
and stay put), or a password change is owed (go to that screen and nowhere else). Prose
cannot be switched on — it is written for a person, it gets reworded, and a frontend that
matched on it would break silently the first time somebody improved a sentence.

So every 403 carries a stable `code` beside its `detail`. `detail` stays a plain string, so
the existing client-side error reader is unchanged and FastAPI's own 403s (there are none
left, but a dependency could add one) still parse.
"""

from fastapi import HTTPException

ADMIN_MODE_REQUIRED = "admin_mode_required"
CAPABILITY_REQUIRED = "capability_required"
PASSWORD_CHANGE_REQUIRED = "password_change_required"


class Forbidden(HTTPException):
    """A 403 that says which kind it is. `main.py` registers the handler that emits `code`."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(status_code=403, detail=detail)
        self.code = code
