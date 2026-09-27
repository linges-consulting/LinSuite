"""Celery application. Workers run `celery -A core.celery_app worker`.

Tasks live in each domain's `tasks.py` and are registered by `include`.

The broker URL is read in `on_configure` rather than at import, because importing this module
must not require a configured environment: `auth/passwords.py` imports a task, `main.py`
imports that, and a web process that cannot be imported without a broker URL is a web process
whose tests have to have one before collection. Celery evaluates its config lazily on first
use, which is exactly when the setting is wanted.
"""

from celery import Celery
from celery.schedules import crontab
from celery.signals import setup_logging, worker_init

from core.config import get_settings
from core.logging import configure_logging

celery_app = Celery(
    "linsuite", include=["core.tasks", "customers.tasks", "forms.tasks", "notifications.tasks"]
)


@celery_app.on_configure.connect
def _configure(sender: Celery, **_: object) -> None:
    settings = get_settings()
    sender.conf.broker_url = settings.redis_url
    sender.conf.result_backend = settings.redis_url
    sender.conf.task_acks_late = True
    # Run by the one `beat` service (infra/compose.yaml) — never `worker -B`, so scaling the
    # worker never runs two schedulers. Beat only enqueues; the worker does the work.
    sender.conf.timezone = "UTC"
    sender.conf.beat_schedule = {
        "reconcile-form-archives": {
            "task": "forms.tasks.reconcile_archives",
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
    }


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
