"""S4: the archived record contains the pinned version's visible, escaped content."""

import uuid
from datetime import UTC, datetime

import pytest

from core.models import Business
from forms.models import FormTemplateVersion
from tests.test_form_submissions import SIGNED
from tests.test_forms import PREGNANT, SIGNATURE, WEEKS, schema


def test_archived_html_keeps_visible_answers_signature_branding_and_record_identity():
    from forms.render import render_html

    version = FormTemplateVersion(name="Prenatal intake", number=1, schema=schema())
    business = Business(name="Cedar & Lane", timezone="America/Toronto")
    submission_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    html = render_html(
        version,
        {PREGNANT: "no", WEEKS: "<script>private hidden answer</script>", SIGNATURE: SIGNED},
        business,
        submitted_at=datetime(2026, 9, 22, 14, tzinfo=UTC),
        submission_id=submission_id,
        source_ip="127.0.0.1",
        logo_uri=SIGNED["image"],
    )
    assert "Cedar &amp; Lane" in html
    assert "Are you pregnant?" in html
    assert "How many weeks?" not in html
    assert "private hidden answer" not in html
    assert SIGNED["image"] in html
    assert "Priya Nair" in html
    assert "Version 1" in html and str(submission_id) in html
    assert "2026-09-22 10:00" in html and "America/Toronto" in html


def test_visible_untrusted_answers_are_escaped():
    from forms.render import render_html

    html = render_html(
        FormTemplateVersion(name="Intake", number=1, schema=schema()),
        {PREGNANT: "yes", WEEKS: "<script>alert('x')</script>", SIGNATURE: SIGNED},
        Business(name="Cedar", timezone="America/Toronto"),
        submitted_at=datetime(2026, 9, 22, 14, tzinfo=UTC),
        submission_id=uuid.uuid4(),
    )
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


@pytest.mark.parametrize("blank", [False, True])
def test_archives_and_paper_forms_keep_the_pinned_help_wording(blank):
    from forms.render import render_html

    definition = schema()
    definition["fields"][0]["help"] = "Read <carefully> before answering."
    definition["fields"][-1]["help"] = "Signing confirms <agreement>."
    definition["fields"].append(
        {
            "key": str(uuid.uuid4()),
            "type": "acknowledgement",
            "label": "I agree",
            "help": "Terms <as published>.",
        }
    )
    html = render_html(
        FormTemplateVersion(name="Intake", number=1, schema=definition),
        {PREGNANT: "no", SIGNATURE: SIGNED},
        Business(name="Cedar", timezone="America/Toronto"),
        blank=blank,
    )
    assert "Read &lt;carefully&gt; before answering." in html
    assert "Signing confirms &lt;agreement&gt;." in html
    assert "Terms &lt;as published&gt;." in html


@pytest.mark.parametrize(
    "url", ["http://localhost/private", "https://example.com", "file:///etc/passwd"]
)
def test_pdf_renderer_cannot_fetch_network_or_local_files(url):
    from forms.render import data_only_fetcher

    with pytest.raises(ValueError, match="data"):
        data_only_fetcher(url)


def test_pdf_renderer_accepts_embedded_data():
    from forms.render import data_only_fetcher

    response = data_only_fetcher("data:text/plain;base64,SGVsbG8=")
    try:
        assert response.content_type == "text/plain"
        assert response.read() == b"Hello"
    finally:
        response.close()
