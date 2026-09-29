"""Celery application. Workers run `celery -A core.celery_app worker`.

Tasks live in each domain's `tasks.py` and are registered by `include`.

The broker URL is read in `on_configure` rather than at import, because importing this module
must not require a configured environment: `auth/passwords.py` imports a task, `main.py`
imports that, and a web process that cannot be imported without a broker URL is a web process
whose tests have to have one before collection. Celery evaluates its config lazily on first
use, which is exactly when the setting is wanted.

**`beat_schedule`/`timezone`/`task_acks_late` are set directly below, at import time, never
inside `on_configure`.** They were originally in the same lazy handler as `broker_url` — but
`on_configure` fires exactly once per app, whenever *anything* first touches `.conf` (including
merely importing a `tasks.py` module that decorates a task, which several test files do at
collection time, before `conftest.py`'s fixtures have set `DATABASE_URL`/`REDIS_URL` in the
environment). A collection-time firing still runs `_configure` to completion, but if the app's
config later gets rebuilt for real once the environment is actually ready, `on_configure` never
fires again to restore it — so `beat_schedule` silently reverted to Celery's own empty default
for the rest of the process (a real, order-dependent CI failure, not a test flake to wave
through: `tests/test_partitions.py::test_beat_schedules_maintain_partitions_nightly`). None of
these three actually depend on `get_settings()`, so there is no reason for them to share the
lazy handler's fragility — only `broker_url`/`result_backend` genuinely need the environment.
"""

from celery import Celery
from celery.schedules import crontab
from celery.signals import setup_logging, worker_init

from core.config import get_settings
from core.logging import configure_logging

celery_app = Celery(
    "linsuite",
    include=[
        "billing.commission_report",
        "billing.documents",
        "core.tasks",
        "customers.tasks",
        "forms.tasks",
        "notifications.tasks",
        "notifications.reminders",
    ],
)

celery_app.conf.task_acks_late = True
# Run by the one `beat` service (infra/compose.yaml) — never `worker -B`, so scaling the
# worker never runs two schedulers. Beat only enqueues; the worker does the work.
celery_app.conf.timezone = "UTC"
celery_app.conf.beat_schedule = {
    "reconcile-form-archives": {
        "task": "forms.tasks.reconcile_archives",
        "schedule": 300.0,
    },
    # Issued invoices / checkout-complete treatment receipts whose render never landed.
    "reconcile-invoice-documents": {
        "task": "billing.documents.reconcile_renders",
        "schedule": 300.0,
    },
    # Nightly at 03:15 UTC. Idempotent and a no-op on all but one night a year, so the
    # hour does not matter.
    "maintain-partitions": {
        "task": "core.tasks.maintain_partitions",
        "schedule": crontab(hour=3, minute=15),
    },
    # Nightly at 03:30 UTC (late evening across North America): finish erasure requests
    # whose hold has passed, and shred the keys of every client whose hold has expired.
    # Idempotent — a missed night is caught up by the next.
    "purge-expired": {
        "task": "customers.tasks.purge_expired",
        "schedule": crontab(hour=3, minute=30),
    },
    # Every 15 minutes: fine enough granularity for the shortest default reminder interval
    # (two hours before) without scanning the appointments table continuously. Idempotent
    # (`appointment_reminders`' unique constraint) — a run that overlaps the previous one,
    # or that catches up after a missed one, sends each interval at most once.
    "send-appointment-reminders": {
        "task": "notifications.send_appointment_reminders",
        "schedule": crontab(minute="*/15"),
    },
}


@celery_app.on_configure.connect
def _configure(sender: Celery, **_: object) -> None:
    settings = get_settings()
    sender.conf.broker_url = settings.redis_url
    sender.conf.result_backend = settings.redis_url


@worker_init.connect
def _require_the_purge_dsn(**_: object) -> None:
    """A worker runs the purge tasks, so it fails at boot without the purge DSN. Otherwise it
    would start and only fail on the first nightly run. The web process never needs the DSN,
    and beat only enqueues, so neither checks."""
    from core.db import purge_dsn

    purge_dsn()


@setup_logging.connect
def _configure_worker_logging(**_: object) -> None:
    configure_logging(get_settings().log_level)
