import pytest

from vitality_bss.extract import (
    Fields,
    extract_fields,
    is_valid_iso_date,
    missing_fields,
    normalize_date,
    parse_amount,
)

FULL = (
    "Patient Name: Jane Doe\nPatient ID: PT-9001\nClaim Number: CLM-2026-0099\n"
    "Service Date: 09/01/2026\nBilled Amount: $1,234.56\nInsurer: Cigna\n"
)


def test_extract_full_document():
    assert extract_fields(FULL) == {
        "patient_name": "Jane Doe",
        "patient_id": "PT-9001",
        "claim_number": "CLM-2026-0099",
        "service_date": "2026-09-01",
        "billed_amount": 1234.56,
        "insurer": "Cigna",
    }


def test_extract_empty():
    f = extract_fields("nothing to see here")
    assert f == {
        "patient_name": "",
        "patient_id": "",
        "claim_number": "",
        "service_date": "",
        "billed_amount": None,
        "insurer": "",
    }
    assert extract_fields(None)["patient_id"] == ""  # type: ignore[arg-type]


def test_generic_identifiers():
    f = extract_fields("MRN: ab-55512\nClaim No: X12345\nMember: Ana Cruz")
    assert f["patient_id"] == "AB-55512"
    assert f["claim_number"] == "X12345"
    assert f["patient_name"] == "Ana Cruz"


def test_member_id_and_account_number():
    assert extract_fields("Member ID: M998877")["patient_id"] == "M998877"
    assert extract_fields("Account #: 445566")["patient_id"] == "445566"


def test_name_requires_two_words_and_strips_stopwords():
    assert extract_fields("Patient Name: Cher")["patient_name"] == ""
    assert extract_fields("Patient Name: Ana Cruz ID")["patient_name"] == "Ana Cruz"
    assert extract_fields("Subscriber Name: Maria Santos")["patient_name"] == "Maria Santos"


def test_name_does_not_cross_lines():
    assert extract_fields("Patient Name: Ana Cruz\nPatient ID: PT-1234")["patient_name"] == "Ana Cruz"


def test_service_date_variants():
    assert extract_fields("Date of Service: 1/2/26")["service_date"] == "2026-01-02"
    assert extract_fields("DOS - 2026-03-04")["service_date"] == "2026-03-04"
    assert extract_fields("Specimen Collected: 08/13/2026")["service_date"] == "2026-08-13"
    assert extract_fields("Visit Date: 02/28/2026")["service_date"] == "2026-02-28"


def test_service_date_skips_date_of_birth():
    assert extract_fields("DOB: 01/01/1980\nSeen 05/06/2026")["service_date"] == "2026-05-06"


def test_service_date_fallback_first_date():
    assert extract_fields("printed 07/04/2026 and 08/04/2026")["service_date"] == "2026-07-04"


def test_invalid_date_is_blank():
    assert extract_fields("Service Date: 13/45/2026")["service_date"] == ""


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("Billed Amount: $1,250.00", 1250.0),
        ("Total Charges: 99.10", 99.10),
        ("Total Due - $5.00", 5.0),
        ("Balance Due $12.34", 12.34),
        ("Amount Due: 7.50", 7.5),
        ("you owe $42.00 today", 42.0),
    ],
)
def test_amount_variants(text, amount):
    assert extract_fields(text)["billed_amount"] == amount


def test_billed_amount_preferred_over_other_amounts():
    text = "Allowed Amount: $980.00\nBilled Amount: $1,250.00\nAmount Due: $5.00"
    assert extract_fields(text)["billed_amount"] == 1250.0


def test_insurer_detection():
    assert extract_fields("covered by Aetna")["insurer"] == "Aetna"
    assert extract_fields("BLUECROSS BLUESHIELD plan")["insurer"] == "BlueCross BlueShield"
    assert extract_fields("PhilHealth member")["insurer"] == "PhilHealth"


@pytest.mark.parametrize(
    ("raw", "iso"),
    [
        ("09/01/2026", "2026-09-01"),
        ("9/1/26", "2026-09-01"),
        ("2026-09-01", "2026-09-01"),
        (" 2026-09-01 ", "2026-09-01"),
        ("2026/09/01", ""),
        ("", ""),
        ("garbage", ""),
    ],
)
def test_normalize_date(raw, iso):
    assert normalize_date(raw) == iso


def test_normalize_date_none():
    assert normalize_date(None) == ""  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        ("$1,234.50", 1234.5),
        ("12", 12.0),
        (12, 12.0),
        (3.14159, 3.14),
        ("0", 0.0),
        (" $ 5 ", 5.0),
        ("-1", None),
        (-0.5, None),
        ("abc", None),
        ("", None),
        ("  ", None),
        (None, None),
        (True, None),
        (float("nan"), None),
        (float("inf"), None),
        ("inf", None),
    ],
)
def test_parse_amount(raw, value):
    assert parse_amount(raw) == value


def _fields(**over):  # type: ignore[no-untyped-def]
    base = Fields(
        patient_name="A B", patient_id="PT-1", claim_number="", service_date="2026-01-01", billed_amount=1.0, insurer=""
    )
    base.update(over)  # type: ignore[typeddict-item]
    return base


def test_missing_fields_by_type():
    assert missing_fields(_fields(), "EOB") == []
    assert missing_fields(_fields(patient_id=""), "Lab Report") == ["patient_id"]
    assert missing_fields(_fields(patient_name=""), "Clinical Note") == ["patient_name"]
    assert missing_fields(_fields(billed_amount=None, service_date=""), "Invoice") == ["service_date", "billed_amount"]
    assert missing_fields(_fields(billed_amount=None, service_date=""), "EOB") == ["service_date", "billed_amount"]
    assert missing_fields(_fields(billed_amount=None, service_date=""), "Lab Report") == []
    assert missing_fields(_fields(billed_amount=0.0), "EOB") == []


def test_is_valid_iso_date():
    assert is_valid_iso_date("2026-02-28")
    assert not is_valid_iso_date("2026-02-30")
    assert not is_valid_iso_date("02/02/2026")
