"""Celery application. Workers run `celery -A core.celery_app worker`.

Tasks live in each domain's `tasks.py` and are registered by `include`.
"""

from celery import Celery
from celery.signals import setup_logging

from core.config import get_settings
from core.logging import configure_logging

celery_app = Celery(
    "linsuite",
    broker=get_settings().redis_url,
    backend=get_settings().redis_url,
    include=["core.tasks"],
)
celery_app.conf.task_acks_late = True


@setup_logging.connect
def _configure_worker_logging(**_: object) -> None:
    configure_logging(get_settings().log_level)
