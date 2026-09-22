"""Form work that must not run in a request: rendering a submission's PDF (Task 5, #48).

Enqueued by `POST /api/public/forms/submit` after its commit. A stub until Task 5 fills it:
it will load the submission, open the answers with `existing_key` (never `data_key` — a
render for a shredded client is a silent no-op), render, and `store_document`.
"""

from core.celery_app import celery_app


@celery_app.task(name="forms.tasks.render_submission")
def render_submission(submission_id: str) -> None:
    """Task 5 renders the PDF. Nothing yet."""
