"""The HTML is the record's content seam; PDF conversion cannot fetch outside assets."""

import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from jinja2 import Environment

from core.models import Business
from forms.models import FormTemplateVersion
from forms.schema import FormSchema, visible_keys

_ENV = Environment(autoescape=True)
_TEMPLATE = _ENV.from_string("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{{ version.name }}</title>
<style>
@page { size: Letter; margin: 20mm; }
body { font: 11pt sans-serif; color: #18212f; line-height: 1.5; }
header img { max-height: 50px; max-width: 200px; }
h1 { font-size: 20pt; } h2 { font-size: 14pt; }
.field { margin: 18px 0; break-inside: avoid; }
.label { font-weight: bold; white-space: pre-wrap; }
.answer, .help { white-space: pre-wrap; }
.writing { border-bottom: 1px solid #999; height: 32px; }
@media print { body { color: black; } }
.signature { max-width: 300px; height: 100px; object-fit: contain; }
footer { margin-top: 30px; font-size: 9pt; border-top: 1px solid #ccc; padding-top: 10px; }
</style></head><body>
<header>{% if logo_uri %}<img src="{{ logo_uri }}" alt="">{% endif %}
<h2>{{ business.name }}</h2></header><h1>{{ version.name }}</h1>
{% for field, value in fields %}
<section class="field">
{% if field.type == 'heading' %}<h2>{{ field.label }}</h2>
{% elif field.type == 'paragraph' %}<p class="answer">{{ field.label }}</p>
{% else %}<div class="label">{{ field.label }}</div>
{% if blank %}
{% if field.show_if %}<p class="help">Complete if the earlier answer is
{{ field.show_if.equals | join(', ') }}.</p>{% endif %}
{% if field.options %}<p>{{ field.options | join(' / ') }}</p>{% endif %}
<div class="writing"></div>
{% elif field.type == 'signature' and value %}
<img class="signature" src="{{ value.image }}" alt="Signature">
<div>{{ value.name }}</div>
{% else %}<div class="answer">{{ value }}</div>{% endif %}
{% endif %}
{% if field.help %}<p class="help">{{ field.help }}</p>{% endif %}
</section>{% endfor %}
<footer>{{ version.name }} · Version {{ version.number }}
{% if blank %}<div>Client: ________</div>
{% else %}<div>Submission {{ submission_id }}</div>
<div>Submitted {{ local_time }} ({{ business.timezone }})</div>
{% if source_ip %}<div>Source IP: {{ source_ip }}</div>{% endif %}
{% endif %}
</footer></body></html>""")


def render_html(
    version: FormTemplateVersion,
    answers: dict[str, Any],
    business: Business,
    *,
    submitted_at: datetime | None = None,
    submission_id: uuid.UUID | None = None,
    source_ip: str | None = None,
    logo_uri: str | None = None,
    blank: bool = False,
) -> str:
    """Render only the fields visible under the pinned version, with autoescaping."""
    schema = FormSchema.model_validate(version.schema)
    shown = set(visible_keys(schema, answers))
    fields = []
    for field in schema.fields:
        if not blank and field.key not in shown:
            continue
        value = answers.get(field.key, "")
        if field.type == "yes_no":
            value = {"yes": "Yes", "no": "No"}.get(value, "")
        elif field.type == "multi_choice":
            value = ", ".join(value) if isinstance(value, list) else ""
        elif field.type == "acknowledgement":
            value = "Agreed" if value else ""
        fields.append((field, value))
    return _TEMPLATE.render(
        version=version,
        business=business,
        fields=fields,
        submission_id=submission_id,
        local_time=submitted_at.astimezone(ZoneInfo(business.timezone)).strftime("%Y-%m-%d %H:%M")
        if submitted_at
        else "",
        source_ip=source_ip,
        logo_uri=logo_uri,
        blank=blank,
    )


def _fetcher():
    from weasyprint.urls import URLFetcher

    return URLFetcher(allowed_protocols={"data"}, allow_redirects=False)


def data_only_fetcher(url: str):
    """Local embedded images only: a PDF never reads the network or the filesystem."""
    if urlsplit(url).scheme != "data":
        raise ValueError("Only embedded data: resources are permitted")
    return _fetcher()(url)


def html_to_pdf(html: str) -> bytes:
    from weasyprint import HTML

    return HTML(string=html, url_fetcher=_fetcher()).write_pdf()
