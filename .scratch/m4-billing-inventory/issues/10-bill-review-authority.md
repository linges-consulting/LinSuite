# 10 — Bill review authority: staff-request + inline admin edit

**What to build:** The two independently-configurable override paths for anything staff can't
resolve alone on #09's screen: (a) staff submits an ad hoc discount or price override for
admin/owner review, who approves or revises it, and staff resumes billing; (b) an admin/owner
authenticates with their own credentials directly on the staff screen, edits the bill under
their own identity, and that authority ends on save/leave or when the admin-mode window
expires. Both default enabled and are independently toggleable business settings, enforced at
the API, not just hidden in the UI. (#54 stories 9-17, 27-29; the "Review authority" and
"Inline authority" implementation-decision bullets.)

**Blocked by:** 09 (a bill with discounts/totals to request a change against)

**Status:** ready-for-agent

- [ ] Staff can submit a requested price override or ad hoc discount; it's persisted with
      requester, the affected draft's revision, and timestamp — pending admin decision
- [ ] Admin/owner reviewing a request can approve as-is or revise it; either way, staff can
      resume ordinary billing on the same draft afterward
- [ ] A stale approval (draft has moved on) cannot be used to authorize a different exceptional
      change than the one actually reviewed
- [ ] Admin/owner can authenticate directly on the staff screen (own credentials, existing
      MFA policy, no bypass) and edit the bill; the resulting change is attributed to the
      admin/owner, not to the staff account whose screen it was
- [ ] That inline authority ends on save, on leaving the bill, or when the admin-mode window
      expires — it never becomes a persistent elevated staff session
- [ ] Both paths are gated by their own business-setting toggle, default enabled, enforced
      server-side (a direct API call with the toggle off is refused, not just hidden)
- [ ] Disabling one path never removes the admin/owner's own ordinary billing access
