import math

from vitality_bss.dedupe import FINGERPRINT_DIM, MIN_TOKENS, best_match, cosine, fingerprint, tokens

TEXT = "Explanation of benefits for patient PT-1001 claim CLM-2026-0042 billed amount 1250.00 humana"


def test_tokens():
    assert tokens("Hello, World! 42") == ["hello", "world", "42"]
    assert tokens(None) == []  # type: ignore[arg-type]


def test_fingerprint_shape_and_norm():
    fp = fingerprint(TEXT)
    assert fp is not None
    assert len(fp) == FINGERPRINT_DIM == 512
    assert math.isclose(math.sqrt(sum(v * v for v in fp)), 1.0, rel_tol=1e-4)
    assert all(v >= 0 for v in fp)


def test_fingerprint_deterministic_and_case_insensitive():
    assert fingerprint(TEXT) == fingerprint(TEXT.upper())


def test_fingerprint_counts_repeats():
    a = fingerprint("alpha alpha alpha beta gamma delta epsilon zeta eta")
    b = fingerprint("alpha beta gamma delta epsilon zeta eta theta iota")
    assert a and b
    assert max(a) > max(b)


def test_fingerprint_too_short():
    assert MIN_TOKENS == 8
    assert fingerprint("one two three four five six seven") is None
    assert fingerprint("one two three four five six seven eight") is not None


def test_cosine():
    assert cosine([1, 0], [1, 0]) == 1.0
    assert cosine([1, 0], [0, 1]) == 0.0
    assert math.isclose(cosine([1, 1], [1, 0]), 1 / math.sqrt(2))
    assert math.isclose(cosine([2, 0], [1, 0]), 1.0)
    assert cosine([], []) == 0.0
    assert cosine([1, 2], [1]) == 0.0
    assert cosine([0, 0], [1, 1]) == 0.0
    assert cosine([1, 1], [0, 0]) == 0.0


def test_near_duplicate_similarity():
    a = fingerprint(TEXT)
    b = fingerprint(TEXT + " page 1")
    c = fingerprint("laboratory report hemoglobin glucose cholesterol reference range specimen collected")
    assert a and b and c
    assert cosine(a, b) > 0.9
    assert cosine(a, c) < 0.5


def test_best_match():
    a = [1.0, 0.0]
    others = [(1, [0.0, 1.0]), (2, None), (3, [1.0, 0.1]), (4, [1.0, 0.0])]
    assert best_match(a, others, 0.9) == (4, 1.0)
    assert best_match(a, [(4, [1.0, 0.0]), (3, [1.0, 0.1])], 0.9) == (4, 1.0)
    assert best_match(a, [(1, [0.0, 1.0])], 0.9) is None
    assert best_match(a, others, 0.0) is None
    assert best_match(a, [(7, [1.0, 0.0])], 1.0) == (7, 1.0)
    assert best_match(a, [], 0.5) is None
    assert best_match(a, [(8, [])], 0.5) is None


def test_bucket_is_stable_golden():
    from vitality_bss.dedupe import bucket

    assert [bucket(t) for t in ["alpha", "pt", "1001"]] == [154, 417, 13]


def test_fingerprint_golden_values():
    fp = fingerprint("alpha alpha beta gamma delta epsilon zeta eta")
    assert fp is not None
    nonzero = [(i, v) for i, v in enumerate(fp) if v]
    assert nonzero == [
        (14, 0.316228),
        (154, 0.632456),
        (173, 0.316228),
        (174, 0.316228),
        (197, 0.316228),
        (230, 0.316228),
        (245, 0.316228),
    ]


def test_best_match_tie_keeps_first():
    assert best_match([1.0, 0.0], [(5, [1.0, 0.0]), (6, [1.0, 0.0])], 0.5) == (5, 1.0)
