"""S2: the form schema format and the answer rules (`forms/schema.py`).

Every case comes from `app/shared/form-schema-cases.json`, which the frontend suite
(`tests/form-schema.test.ts`, for `lib/forms.ts`) reads too — the builder preview, the fill
page and the server apply one set of rules, and a case added for one is a case for both.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from forms.schema import FormSchema, kept_answers, kind_problems, validate_answers, visible_keys

CASES = json.loads(
    (Path(__file__).resolve().parents[2] / "shared" / "form-schema-cases.json").read_text()
)
BASE = FormSchema.model_validate(CASES["base"])


@pytest.mark.parametrize("case", CASES["schemas"], ids=lambda c: c["name"])
def test_schema_cases(case):
    def problems() -> list[str]:
        try:
            schema = FormSchema.model_validate(case["schema"])
        except ValidationError as error:
            return [str(error)]
        return kind_problems(schema, case.get("kind", "other"))

    if case["valid"]:
        assert problems() == []
    else:
        assert problems() != []


@pytest.mark.parametrize("case", CASES["answers"], ids=lambda c: c["name"])
def test_answer_cases(case):
    assert visible_keys(BASE, case["answers"]) == case["visible"]
    assert list(kept_answers(BASE, case["answers"])) == case["kept"]
    assert validate_answers(BASE, case["answers"]) == case["errors"]


def test_keys_are_stored_lower_case_so_one_field_is_never_two():
    schema = FormSchema.model_validate(
        {
            "fields": [
                {
                    "key": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
                    "type": "short_text",
                    "label": "Name",
                    "required": False,
                }
            ]
        }
    )
    assert schema.fields[0].key == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def test_an_unknown_property_on_a_field_is_refused():
    with pytest.raises(ValidationError):
        FormSchema.model_validate(
            {
                "fields": [
                    {
                        "key": "11111111-1111-4111-8111-111111111111",
                        "type": "short_text",
                        "label": "Name",
                        "required": False,
                        "script": "alert(1)",
                    }
                ]
            }
        )


def test_there_is_a_ceiling_on_the_number_of_fields():
    import uuid

    fields = [
        {"key": str(uuid.uuid4()), "type": "short_text", "label": "Q", "required": False}
        for _ in range(201)
    ]
    with pytest.raises(ValidationError):
        FormSchema.model_validate({"fields": fields})
