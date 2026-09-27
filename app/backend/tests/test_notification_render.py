"""S2: `render` is stdlib `string.Template.safe_substitute`, nothing more — no database."""

from notifications.render import render


def test_render_substitutes_known_placeholders():
    rendered = render(
        "Hi $client_name, your $service_name is at $appointment_time.",
        {"client_name": "Sam", "service_name": "Massage", "appointment_time": "3pm Tuesday"},
    )
    assert rendered == "Hi Sam, your Massage is at 3pm Tuesday."


def test_render_leaves_an_unrecognised_placeholder_literal_instead_of_raising():
    # A merge field that doesn't apply to this trigger (e.g. $form_link on a confirmation)
    # must never break a send — this is the whole reason `safe_substitute` was chosen over
    # `substitute` (which raises `KeyError` on exactly this input).
    rendered = render("Hi $client_name, link: $form_link", {"client_name": "Sam"})
    assert rendered == "Hi Sam, link: $form_link"


def test_render_does_not_execute_expressions():
    # `string.Template` has no loops, conditionals or attribute access to exploit in
    # admin-edited, untrusted-ish template text — a non-identifier construct is left alone.
    rendered = render("$greeting ${not a valid identifier}", {"greeting": "Hi"})
    assert rendered == "Hi ${not a valid identifier}"


def test_render_is_a_pure_function_of_its_arguments():
    template = "Hi $client_name"
    context = {"client_name": "Sam"}
    assert render(template, context) == render(template, dict(context))
    # The context dict passed in is never mutated.
    assert context == {"client_name": "Sam"}
