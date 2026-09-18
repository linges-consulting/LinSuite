"""Celery application. Workers run `celery -A core.celery_app worker`.

Tasks live in each domain's `tasks.py` and are registered by `include`.

The broker URL is read in `on_configure` rather than at import, because importing this module
must not require a configured environment: `auth/passwords.py` imports a task, `main.py`
imports that, and a web process that cannot be imported without a broker URL is a web process
whose tests have to have one before collection. Celery evaluates its config lazily on first
use, which is exactly when the setting is wanted.
"""

from celery import Celery
from celery.signals import setup_logging

from core.config import get_settings
from core.logging import configure_logging

celery_app = Celery("linsuite", include=["core.tasks", "notifications.tasks"])


@celery_app.on_configure.connect
def _configure(sender: Celery, **_: object) -> None:
    settings = get_settings()
    sender.conf.broker_url = settings.redis_url
    sender.conf.result_backend = settings.redis_url
    sender.conf.task_acks_late = True


@setup_logging.connect
def _configure_worker_logging(**_: object) -> None:
    configure_logging(get_settings().log_level)
