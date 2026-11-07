from __future__ import annotations

import pytest

from plamo_cp.engine import TERMS, default_glossary, translation_options, validate_glossary
from plamo_cp.llama_backend import build_prompt


def test_omitted_glossary_keeps_default_prompt_but_explicit_empty_removes_user_terms():
    text = "Find the <plm-a>smallest</plm-a> integer in the array [[PLM_0]]."
    baseline = build_prompt(text, "English", "Japanese")
    assert build_prompt(text, "English", "Japanese", glossary=None) == baseline
    assert "<|plamo:index|>modular inverse" in baseline
    cleared = build_prompt(text, "English", "Japanese", glossary=[])
    assert "<|plamo:index|>modular inverse" not in cleared
    assert "<|plamo:index|><math-a/><|plamo:item|><math-a/>" in cleared


@pytest.mark.parametrize(
    "text,pairs",
    [
        (
            "A proper prefix of a word is edgy.",
            [
                {"source": "PROPER PREFIX", "target": "真接頭辞"},
                {"source": "edge", "target": "端"},
                {"source": "vertex", "target": "点"},
            ],
        ),
        ("C++x and myC++.", [{"source": "C++", "target": "シープラスプラス"}]),
    ],
)
def test_explicit_dictionary_is_authoritative_without_word_filtering(text, pairs):
    options = translation_options(text, "English", "Japanese", glossary=pairs)
    assert options["glossary"] == pairs


def test_general_documents_apply_user_dictionary_without_cp_defaults():
    options = translation_options(
        "The edge of the array.",
        "English",
        "Japanese",
        document_type="general",
        glossary=[{"source": "edge", "target": "端"}],
    )
    assert options["glossary"] == [{"source": "edge", "target": "端"}]
    assert "general document" in options["context"]


def test_configured_phrases_are_passed_alongside_identity_tags_and_math():
    options = translation_options(
        "A <plm-a>strictly\n <plm-b>increasing</plm-b></plm-a> array [[PLM_0]].",
        "English",
        "Japanese",
        glossary=[{"source": "strictly increasing", "target": "狭義単調増加"}],
    )
    terms = options["glossary"]
    assert terms[-1] == {"source": "strictly increasing", "target": "狭義単調増加"}
    assert {
        pair["source"]: pair["target"] for pair in terms if pair["source"].startswith(("<", "[["))
    } == {
        "<plm-a>": "<plm-a>",
        "<plm-b>": "<plm-b>",
        "</plm-b>": "</plm-b>",
        "</plm-a>": "</plm-a>",
        "[[PLM_0]]": "[[PLM_0]]",
    }


def test_validation_copies_trims_and_uses_last_casefolded_source():
    pairs = [{"source": " Array ", "target": " 配列 "}, {"source": "array", "target": "数列"}]
    assert validate_glossary(pairs) == [{"source": "array", "target": "数列"}]
    assert pairs[0] == {"source": " Array ", "target": " 配列 "}


@pytest.mark.parametrize(
    "value", ["<|plamo:tag|>", "<|PLAMO:msg|>", "[[PLM_0]]", "<plm-a>", "</PLM-a>"]
)
@pytest.mark.parametrize("side", ["source", "target"])
def test_reserved_tokens_are_rejected_on_either_side(value, side):
    pair = {"source": "array", "target": "数列", side: value}
    with pytest.raises(ValueError, match="識別子"):
        validate_glossary([pair])


@pytest.mark.parametrize(
    "value",
    [
        True,
        {},
        [{"source": "array"}],
        [{"source": "array", "target": 1}],
        [{"source": " ", "target": "数列"}],
        [{"source": "array", "target": ""}],
        [{"source": "a", "target": "b"}] * 65,
        [{"source": "a", "target": "b" * 6000}],
    ],
)
def test_invalid_glossary_is_rejected(value):
    with pytest.raises(ValueError):
        validate_glossary(value)


def test_custom_terms_reach_windows_pfml3_glossary_without_implicit_defaults():
    prompt = build_prompt(
        "Parallel edges in an array [[PLM_0]].",
        "en",
        "ja",
        glossary=[
            {"source": "array", "target": "数列"},
            {"source": "parallel edge", "target": "多重辺"},
        ],
    )
    assert "<|plamo:index|>array<|plamo:item|>数列" in prompt
    assert "<|plamo:index|>parallel edge<|plamo:item|>多重辺" in prompt
    assert "<|plamo:index|>modular inverse" not in prompt
    assert "<|plamo:index|><math-a/><|plamo:item|><math-a/>" in prompt


def test_default_glossary_contains_canonical_competitive_programming_terms():
    assert TERMS == {
        "modular inverse": "逆元",
        "proper prefix": "真の接頭辞",
        "parallel edge": "多重辺",
        "prefix sum": "累積和",
        "jury": "ジャッジ",
        "subarray": "連続部分配列",
        "increment": "インクリメント",
        "pairwise disjoint": "互いに交差しない",
        "sequence": "列",
        "visualizer": "ビジュアライザ",
        "forest": "森",
        "node": "頂点",
    }
    terms = translation_options(
        "Find modular inverses, prefix sums and parallel edges.", "English", "Japanese"
    )["glossary"]
    assert {pair["source"]: pair["target"] for pair in terms} == TERMS


def test_clearing_all_terms_keeps_only_internal_protection_and_does_not_leak_to_next_request():
    text = "<plm-a>Proper prefixes</plm-a> of [[PLM_0]]."
    cleared = translation_options(text, "en", "ja", glossary=[])
    assert cleared["glossary"] == [
        {"source": marker, "target": marker} for marker in ("<plm-a>", "</plm-a>", "[[PLM_0]]")
    ]
    assert translation_options("Proper prefixes.", "en", "ja")["glossary"] == default_glossary()
    edited = [{"source": "proper prefix", "target": "真接頭辞"}]
    assert (
        translation_options("Proper prefixes.", "en", "ja", glossary=edited)["glossary"] == edited
    )
    assert edited == [{"source": "proper prefix", "target": "真接頭辞"}]


def test_default_glossary_is_a_fresh_copy():
    first = default_glossary()
    first[0]["target"] = "changed"
    first.append({"source": "extra", "target": "余分"})
    assert default_glossary() == [
        {"source": source, "target": target} for source, target in TERMS.items()
    ]
