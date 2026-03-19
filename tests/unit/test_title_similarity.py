from src.state.events import compute_title_similarity


def test_exact_match():
    assert compute_title_similarity("Soccer Practice", "Soccer Practice") == 1.0


def test_case_insensitive():
    assert compute_title_similarity("Soccer Practice", "soccer practice") == 1.0


def test_completely_different():
    assert compute_title_similarity("Soccer Practice", "Piano Lesson") == 0.0


def test_partial_overlap():
    sim = compute_title_similarity("Soccer Practice", "Soccer Game")
    # "soccer" is shared, "practice" vs "game" differ
    assert 0.3 < sim < 0.7


def test_high_overlap():
    sim = compute_title_similarity(
        "Sophia's 7th Birthday Party", "Sophia Birthday Party"
    )
    assert sim >= 0.7


def test_empty_strings():
    assert compute_title_similarity("", "") == 1.0


def test_one_empty():
    assert compute_title_similarity("Soccer", "") == 0.0


def test_with_punctuation():
    sim = compute_title_similarity("Emma's Recital!", "Emma Recital")
    assert sim >= 0.7


def test_below_threshold():
    sim = compute_title_similarity("Soccer Practice Tuesday", "Piano Lesson Wednesday")
    assert sim < 0.7
