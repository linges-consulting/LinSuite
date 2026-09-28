"""The one place `string.Template` is touched (owner decision, `m3.md` — never Jinja2 here).

`render` operates on a single raw template string — a `NotificationTemplate` row's
`subject_template` or its `body_template`, called once per field by the trigger functions
(Phase 12 Task 5, not built yet). `safe_substitute` leaves an unmatched `$placeholder` in the
output literally rather than raising: a template body is admin-edited text, and a merge field
that doesn't apply to a given trigger (e.g. `$form_link` in a `booking_confirmation` template)
must never break a send.
"""

from string import Template


def render(template_row: str, context: dict[str, str]) -> str:
    return Template(template_row).safe_substitute(context)
