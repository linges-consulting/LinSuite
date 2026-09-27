"""Render archived forms in the worker, never on the production request path."""

import asyncio
import base64
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from core.celery_app import celery_app
from core.config import get_settings
from core.documents import store_document
from core.models import Business, Document
from customers.keys import existing_key
from forms.models import FormSubmission, FormTemplateVersion
from forms.render import html_to_pdf, render_html
from forms.submissions import open_answers
from settings.models import LOGO, BrandingAsset


@celery_app.task(
    name="forms.tasks.render_submission",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def render_submission(submission_id: str) -> None:
    asyncio.run(_render_submission(uuid.UUID(submission_id)))


@celery_app.task(
    name="forms.tasks.reconcile_archives",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def reconcile_archives() -> None:
    """The committed submission is the durable work record if enqueueing was lost.

    Beat repairs the commit/enqueue gap, including process exits. Bounded batches and the
    renderer's unique source identity keep retries and overlapping runs harmless.
    """
    for submission_id in asyncio.run(_pending_archives()):
        render_submission.delay(str(submission_id))


async def _pending_archives() -> list[uuid.UUID]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as db:
            return list(
                await db.scalars(
                    select(FormSubmission.id)
                    .outerjoin(
                        Document,
                        (Document.source_id == FormSubmission.id)
                        & (Document.kind == "form_submission"),
                    )
                    .where(FormSubmission.method == "link", Document.id.is_(None))
                    .order_by(FormSubmission.submitted_at)
                    .limit(100)
                )
            )
    finally:
        await engine.dispose()


async def _render_submission(submission_id: uuid.UUID) -> None:
    # A fresh engine for this task's event loop. Only the application role is needed.
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db, db.begin():
            submission = await db.get(FormSubmission, submission_id)
            if submission is None or submission.method == "scan":
                return
            if await db.scalar(
                select(Document.id).where(
                    Document.kind == "form_submission", Document.source_id == submission_id
                )
            ):
                return
            key = await existing_key(db, submission.customer_id)
            if key is None:
                return
            answers = await open_answers(db, submission)
            if answers is None:
                return
            version = await db.get(FormTemplateVersion, submission.version_id)
            business = await db.get(Business, 1)
            logo = await db.get(BrandingAsset, LOGO)
            logo_uri = (
                "data:image/png;base64," + base64.b64encode(logo.data).decode() if logo else None
            )
            html = render_html(
                version,
                answers,
                business,
                submitted_at=submission.submitted_at,
                submission_id=submission.id,
                source_ip=str(submission.source_ip) if submission.source_ip else None,
                logo_uri=logo_uri,
            )
            await store_document(
                db,
                key=key,
                customer_id=submission.customer_id,
                kind="form_submission",
                source_id=submission.id,
                content=html_to_pdf(html),
                content_type="application/pdf",
            )
    finally:
        await engine.dispose()
