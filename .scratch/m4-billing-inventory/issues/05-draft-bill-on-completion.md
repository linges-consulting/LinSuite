# 05 — Draft service bill on appointment completion

**What to build:** Completing an appointment automatically creates or contributes a line to one
draft (unissued) service bill for that visit, reusing the existing `booking_group_id` to group
multiple providers/services from the same visit onto one bill. The commission rate is snapshotted
at this moment (not at invoice issue). Everything commits in the same transaction as completion
itself — the existing terminal completion boundary (`complete_appointment`) and its idempotent,
race-safe identity are what prevent duplicate lines on a retried request. Cancellation and
no-show never create a bill line. (#54 stories 1-4, 7, 95; the "Completion transaction" and
"Visit grouping" implementation-decision bullets.)

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] Completing an appointment creates a draft bill (or appends a line to the visit's existing
      open draft, matched by `booking_group_id`) in the same transaction as completion,
      following the same commit boundary `_clear_queue_entry` already hooks into
- [ ] Each line retains its own service, provider and the commission rate snapshotted at this
      exact moment
- [ ] A retried completion request (same appointment, already completed) never creates a
      duplicate line
- [ ] Cancelling or marking an appointment no-show never creates or contributes a bill line
- [ ] A later appointment completing for an already-*issued* bill's visit opens a new draft
      rather than appending to the issued document
- [ ] `tests/test_appointments.py`'s existing completion tests still pass unchanged
