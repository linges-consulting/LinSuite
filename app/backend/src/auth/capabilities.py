"""The capability registry, and the dependency that enforces it.

**Capabilities are code, not data.** A capability only means something because a route asks
for it, so the list of them belongs where the routes are — in this module — and the database
stores only which of these keys a role holds. A free-form permission string would be a
checkbox nobody checks: it would read as granted in the interface and be refused everywhere,
which is the worst of both answers. `role_capabilities.capability` is validated against this
registry at the API boundary for exactly that reason.

**The convention.** A key is `<area>.<verb>`, lowercase, dotted, and never renamed once it
has shipped — a rename is a silent revocation for every role holding the old string. `admin`
is the one bare key, because it names the mode rather than an area. Adding a capability is
adding an entry here and a `Requires(...)` on the routes it gates; nothing else.

**`requires_admin_mode` is a property of the capability, not of the route.** Administering
the business is what PRD §1 puts behind the short Admin Mode window, and deciding that per
route would mean every future route re-deciding it — and one of them getting it wrong. So
the registry carries the rule and `Requires` applies it, which is why an administrative
capability held by a session being served in Staff Mode is still refused.

**Read fresh on every request.** `Requires` resolves through `CurrentUser`, which loads the
role and its capabilities with the user row. Nothing is cached into the session token, so a
capability taken away from a role is gone from every session that role is serving — with no
re-login, and no window in which a revoked permission is still honoured.
"""

from collections.abc import Callable
from dataclasses import dataclass

from auth.models import ADMIN_CAPABILITY, User
from auth.modes import AdminUser
from auth.session import CurrentUser
from core.errors import CAPABILITY_REQUIRED, Forbidden


@dataclass(frozen=True)
class Capability:
    """One permission: its stable key, what it lets somebody do, and where it is shown."""

    key: str
    # Shown beside the toggle. Written for the administrator deciding whether to grant it, so
    # it says what the holder can *do*, not what the code checks.
    description: str
    group: str
    # Administrative: held in any mode, usable only in Admin Mode.
    requires_admin_mode: bool = False


# Only capabilities that gate something already built or imminent. A capability with no
# route behind it is a promise the server does not keep.
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "admin",
        "Enter Admin Mode and change the business's own settings.",
        "Administration",
        requires_admin_mode=True,
    ),
    Capability(
        "roles.manage",
        "Create roles and choose what each one is allowed to do.",
        "Administration",
        requires_admin_mode=True,
    ),
    Capability(
        "users.manage",
        "Add staff members, set their credentials and commission rates, assign roles, and "
        "unlock accounts locked by failed sign-ins.",
        "Administration",
        requires_admin_mode=True,
    ),
    Capability(
        "catalog.manage",
        "Add and change services, products, prices and the resources they need.",
        "Administration",
        requires_admin_mode=True,
    ),
    Capability(
        "audit.view",
        "See who has accessed a client's record.",
        "Administration",
        # Who looked at whom is itself sensitive: the report names a client and everybody
        # who opened their chart, so it sits behind the same short window as the settings.
        requires_admin_mode=True,
    ),
    Capability(
        "forms.manage",
        "Build and publish intake forms, consents and waivers.",
        "Administration",
        requires_admin_mode=True,
    ),
    Capability("schedule.view", "See the appointment calendar.", "Schedule"),
    Capability("schedule.manage", "Book, move and cancel appointments.", "Schedule"),
    Capability(
        "schedule.override_availability",
        "Book outside a staff member's shift, time off or vacation. Rooms and equipment "
        "are never overridable.",
        "Schedule",
    ),
    Capability("customers.view", "Open customer profiles and their history.", "Customers"),
    Capability("customers.manage", "Add customers and edit their details.", "Customers"),
    Capability(
        "customers.erase",
        "Erase a client's personal information on request; what the law holds is kept and "
        "explained.",
        "Customers",
        # Irreversible, so it sits behind the short window like the other compliance action.
        requires_admin_mode=True,
    ),
    # Front-desk work, so Staff Mode: the clinic tablet is never signed in, and staff hand
    # it over by showing the link's QR code (owner ruling, #46).
    Capability("forms.issue", "Send forms to clients and take scans.", "Customers"),
    # Reading a completed form is a PHI access (logged per open); the list is metadata (#47).
    Capability("forms.view", "Open clients' completed forms.", "Customers"),
    Capability("notes.view", "Open clients' session notes and visual markup.", "Customers"),
    Capability(
        "notes.write", "Author, edit and lock your own appointment session notes.", "Customers"
    ),
    Capability(
        "notes.manage",
        "Configure session-note templates.",
        "Administration",
        requires_admin_mode=True,
    ),
    # Front-desk work, so no `requires_admin_mode` — the same reasoning `forms.issue` already
    # gives for the clinic tablet. Every route this gates also 404s when a business has not
    # opted into `enable_walk_in_queue` at all (Phase 7 Task 1, #12) — this capability decides
    # *who*, that toggle decides *whether the surface exists*.
    Capability(
        "queue.manage",
        "Add walk-in clients to the queue, see who is waiting, and mark them as gone.",
        "Queue",
    ),
    # M4's billing family (m4.md): three Wave 1 tickets (#57 tax components, #58 discounts,
    # #60 packages/bundles) each independently named this in their own parallel worktree —
    # merged to one entry at integration. Every later billing-admin surface reuses this exact
    # key rather than minting a near-duplicate. A `billing.view` for staff-facing reads (bill
    # review, #63) is a separate key for whichever ticket first needs one.
    Capability(
        "billing.manage",
        "Configure tax components and rates, discounts, packages, bundles and other billing "
        "configuration.",
        "Billing",
        requires_admin_mode=True,
    ),
    # M4 #61: two distinct, independently grantable keys, rather than one — receiving a
    # delivery and correcting a miscount are different trust decisions, and an owner may want
    # to hand the first to a trusted lead without the second. Both default to admin/owner
    # only. A future sale-driven deduction (#75) never checks either (m4.md, `inventory/
    # stock.py::record_movement` takes no capability at all — only the two routes below do).
    Capability(
        "inventory.receive",
        "Record stock received from a delivery.",
        "Inventory",
        requires_admin_mode=True,
    ),
    Capability(
        "inventory.adjust",
        "Correct a variant's stock count and record why.",
        "Inventory",
        requires_admin_mode=True,
    ),
    # #63: front-desk work (reviewing a visit's own draft bill before checkout), so no
    # `requires_admin_mode` — the same reasoning `queue.manage`/`forms.issue` already give for
    # the front desk. `billing.manage`'s own docstring reserved this exact key for whichever
    # ticket first needed a staff-facing billing read/write, rather than a near-duplicate.
    Capability(
        "billing.view",
        "See a visit's draft service bill and apply eligible discounts before checkout.",
        "Billing",
    ),
    # #69: commission rates and amounts are never staff-facing (m4.md's own note; #54's
    # acceptance criteria) — `audit.view`'s own shape, Administrator-only, Admin Mode.
    Capability(
        "commission.view",
        "See what staff have earned in commission, and export the report.",
        "Billing",
        requires_admin_mode=True,
    ),
)

BY_KEY: dict[str, Capability] = {c.key: c for c in CAPABILITIES}
ALL_KEYS: frozenset[str] = frozenset(BY_KEY)

ADMIN = ADMIN_CAPABILITY


def unknown(keys: list[str]) -> list[str]:
    """The keys in `keys` this registry has never heard of, in the order they were sent."""
    return [key for key in keys if key not in BY_KEY]


def can_switch_modes(user: User) -> bool:
    """Whether the context switcher exists for this account at all (PRD §1).

    Here rather than in `auth/modes.py` because it is a question about the role, not about
    the Redis window: a user whose role does not hold `admin` has no second mode to be in,
    and `/auth/mode` refuses them for the same reason the switcher never renders.
    """
    return ADMIN in user.capabilities


def Requires(key: str) -> Callable:  # noqa: N802 — a dependency factory, named for what it yields
    """The guard a route declares: `dependencies=[Depends(Requires("users.manage"))]`.

    Declared per route and never inferred from a path prefix. A prefix rule is invisible at
    the route it governs, and the first endpoint mounted somewhere unexpected would inherit
    either too much or nothing at all.

    Two shapes, chosen once at import: an administrative capability depends on `AdminUser`,
    which is `require_admin_mode` — so the window is checked, and slid, before the capability
    is. The order matters to the person reading the refusal. Told "your role does not allow
    this" when the truth is "your window lapsed", an administrator goes looking at a role
    they already hold instead of typing their password.
    """
    capability = BY_KEY[key]  # a typo is a startup failure, not a route that silently refuses

    def check(user: User) -> User:
        if key not in user.capabilities:
            raise Forbidden(
                CAPABILITY_REQUIRED,
                f"Your role does not allow this: {capability.description}",
            )
        return user

    if capability.requires_admin_mode:

        async def dependency(user: AdminUser) -> User:
            return check(user)
    else:

        async def dependency(user: CurrentUser) -> User:
            return check(user)

    return dependency
