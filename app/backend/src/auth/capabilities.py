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
