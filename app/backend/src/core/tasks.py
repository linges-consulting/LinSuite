from core.celery_app import celery_app


@celery_app.task
def ping() -> str:
    """Proves the broker -> worker -> result path. Nothing more."""
    return "pong"
