from vitality_bss.classify import (
    ALL_TYPES,
    BINDER_ORDER,
    DOC_TYPES,
    FILENAME_WEIGHT,
    UNCLASSIFIED,
    classify,
    classify_document,
    score_document,
)


def test_doc_types_and_order():
    assert DOC_TYPES == ("EOB", "Clinical Note", "Lab Report", "Invoice", "Insurance Form")
    assert (*DOC_TYPES, UNCLASSIFIED) == ALL_TYPES
    assert sorted(ALL_TYPES, key=BINDER_ORDER.__getitem__) == list(ALL_TYPES)
    assert BINDER_ORDER["EOB"] == 0
    assert BINDER_ORDER[UNCLASSIFIED] == 5


def test_each_type_by_content():
    cases = {
        "EOB": "Explanation of Benefits. Claim Number: CLM-2026-0001. Patient responsibility $50.",
        "Clinical Note": "Chief complaint: cough. Vital signs stable. Assessment and plan: rest.",
        "Lab Report": "Laboratory test result. Specimen adequate. Reference range shown.",
        "Invoice": "Patient billing statement. Amount due $100. Please remit payment.",
        "Insurance Form": "Enrollment form. Policy number P-1. Group number G-2. Subscriber details.",
    }
    for expected, text in cases.items():
        assert classify_document("scan.pdf", text) == expected


def test_filename_hint_weight():
    scores = score_document("eob_march.pdf", "")
    assert scores["EOB"] == FILENAME_WEIGHT
    assert FILENAME_WEIGHT == 4
    assert classify("eob_march.pdf", "random unrelated text").doc_type == "EOB"


def test_filename_outweighs_three_keywords():
    text = "invoice amount due balance due"  # 3 invoice keywords
    assert classify("eob.pdf", text).doc_type == "EOB"  # 4 > 3


def test_keywords_case_insensitive_and_counted():
    scores = score_document("x.pdf", "LABORATORY Specimen GLUCOSE")
    assert scores["Lab Report"] == 3
    assert scores["EOB"] == 0


def test_unclassified_when_nothing_matches():
    res = classify("scan0001.pdf", "lorem ipsum dolor")
    assert res.doc_type == UNCLASSIFIED
    assert res.confidence == 0.0
    assert set(res.scores) == set(DOC_TYPES)


def test_none_inputs_are_safe():
    assert classify(None, None).doc_type == UNCLASSIFIED  # type: ignore[arg-type]


def test_confidence_is_share_of_points():
    res = classify("x.pdf", "deductible copay invoice amount due")
    assert res.doc_type == "EOB"
    assert res.confidence == 0.5
    res = classify("x.pdf", "deductible copay coinsurance invoice")
    assert res.confidence == 0.75


def test_full_confidence_single_type():
    assert classify("x.pdf", "explanation of benefits").confidence == 1.0


def test_tie_breaks_toward_binder_order():
    assert classify("x.pdf", "invoice enrollment").doc_type == "Invoice"
    assert classify("x.pdf", "specimen invoice").doc_type == "Lab Report"


def test_confidence_rounded_to_three_places():
    res = classify("x.pdf", "deductible invoice amount due")
    assert res.doc_type == "Invoice"
    assert res.confidence == 0.667
