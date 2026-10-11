import pytest

from vitality_bss.redact import REDACTED, redact, redact_value


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Patient Name: Jane Doe", "Jane Doe"),
        ("member name = John Q Public", "John Q Public"),
        ("Subscriber Full Name: Maria Santos", "Maria Santos"),
        ("DOB: 01/02/1980", "01/02/1980"),
        ("date of birth: 1980-01-02", "1980-01-02"),
        ("ssn 123-45-6789", "123-45-6789"),
        ("email jane.doe@example.com now", "jane.doe@example.com"),
        ("call (555) 123-4567", "123-4567"),
        ("call +1 555-123-4567", "555-123-4567"),
        ("id PT-1001 here", "PT-1001"),
        ("claim CLM-2026-0042", "CLM-2026-0042"),
        ("MRN: AB12345", "AB12345"),
        ("Patient ID: X9-777", "X9-777"),
        ("Member ID 55512", "55512"),
        ("policy number: AET-884210", "AET-884210"),
        ("Claim No. Z9999", "Z9999"),
    ],
)
def test_redacts_phi(text, secret):
    out = redact(text)
    assert secret not in out
    assert REDACTED in out


def test_keeps_labels_and_non_phi():
    assert redact("Patient Name: Jane Doe") == "Patient Name: [REDACTED]"
    assert redact("MRN: AB12345") == "MRN: [REDACTED]"
    assert redact("DOB: 1/2/80") == "DOB: [REDACTED]"
    assert redact("document processed in 12 ms") == "document processed in 12 ms"
    assert redact("") == ""


def test_name_redaction_stops_at_delimiters():
    assert redact("Patient Name: Jane Doe, status ok") == "Patient Name: [REDACTED], status ok"
    assert redact("patient name: A B\nnext line") == "patient name: [REDACTED]\nnext line"
    assert redact("DOB: 1/2/80; ok") == "DOB: [REDACTED]; ok"


def test_redact_value_recurses():
    value = {"a": "PT-1234", "b": ["x jane@example.com", ("123-45-6789",)], "n": 5, "none": None}
    out = redact_value(value)
    assert out == {"a": REDACTED, "b": [f"x {REDACTED}", (REDACTED,)], "n": 5, "none": None}
    assert isinstance(out["b"][1], tuple)
