"""S2: the shared export mechanism's pure bits (#86) — no database, no client.

The end-to-end behaviour (request, poll, download, 410, the audit event, nightly cleanup,
and a made-up `kind` running through the one shared task) is proven in
`tests/test_commission_report.py`, which already has an admin account and a clean database
per test; this file is only the registry and the expiry predicate in isolation.
"""

import uuid
from datetime import UTC, datetime, timedelta

from core.exports import _builders, is_expired, is_registered, register_builder
from core.models import ReportExport


def _export(*, expires_at: datetime) -> ReportExport:
    export = ReportExport(kind="commission", params={}, requested_by=uuid.uuid4())
    export.expires_at = expires_at
    return export


def test_is_expired_is_a_plain_comparison_against_expires_at():
    now = datetime.now(UTC)
    assert not is_expired(_export(expires_at=now + timedelta(seconds=1)), now=now)
    assert is_expired(_export(expires_at=now - timedelta(seconds=1)), now=now)
    # The boundary itself counts as expired — `expires_at` is the last live instant.
    assert is_expired(_export(expires_at=now), now=now)


def test_registering_a_builder_is_the_whole_registration_surface():
    assert not is_registered("a_kind_nobody_registered_yet")

    async def build(db, params):  # pragma: no cover - never called, only registered
        return ""

    register_builder("a_kind_nobody_registered_yet", build)
    try:
        assert is_registered("a_kind_nobody_registered_yet")
        assert _builders["a_kind_nobody_registered_yet"] is build
    finally:
        _builders.pop("a_kind_nobody_registered_yet", None)
