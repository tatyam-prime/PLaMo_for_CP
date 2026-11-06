from __future__ import annotations

import hashlib

import pytest

from plamo_cp.engine import (
    PROMPT_VERSION,
    TERMS,
    default_glossary,
    translation_options,
    validate_glossary,
)
from plamo_cp.llama_backend import build_prompt


def test_omitted_glossary_keeps_default_prompt_but_explicit_empty_removes_user_terms():
    text = "Find the <plm-a>smallest</plm-a> integer in the array [[PLM_0]]."
    baseline = build_prompt(text, "English", "Japanese")
    assert (
        hashlib.sha256(baseline.encode()).hexdigest()
        == "c18914a41605309d679dbb021ba04ad798bd8a14b6eb82bd70b5d8ad1f502aba"
    )
    assert build_prompt(text, "English", "Japanese", glossary=None) == baseline
    cleared = build_prompt(text, "English", "Japanese", glossary=[])
    assert cleared != baseline
    assert "<|plamo:index|>modular inverse" not in cleared
    assert "<|plamo:index|>[[PLM_0]]<|plamo:item|>[[PLM_0]]" in cleared


def test_explicit_dictionary_is_authoritative_including_terms_absent_from_input():
    pairs = [
        {"source": "PROPER PREFIX", "target": "真接頭辞"},
        {"source": "edge", "target": "端"},
        {"source": "vertex", "target": "点"},
    ]
    options = translation_options(
        "A proper prefix of a word is edgy.", "English", "Japanese", glossary=pairs
    )
    assert options["glossary"] == pairs
    assert pairs[0]["source"] == "PROPER PREFIX"


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


def test_protection_identities_are_separate_from_the_authoritative_user_dictionary():
    text = "Formula [[PLM_0]]."
    pair = {"source": "unrelated phrase", "target": "別の語句"}
    assert translation_options(text, "en", "ja", glossary=[pair])["glossary"] == [
        {"source": "[[PLM_0]]", "target": "[[PLM_0]]"},
        pair,
    ]


def test_symbol_terms_are_passed_as_configured_without_word_filtering():
    options = translation_options(
        "C++ is not C++x or myC++.",
        "en",
        "ja",
        document_type="general",
        glossary=[{"source": "C++", "target": "シープラスプラス"}],
    )
    assert options["glossary"] == [{"source": "C++", "target": "シープラスプラス"}]
    assert translation_options(
        "C++x and myC++",
        "en",
        "ja",
        document_type="general",
        glossary=[{"source": "C++", "target": "wrong"}],
    )["glossary"] == [{"source": "C++", "target": "wrong"}]


def test_absent_configured_lemma_still_reaches_prompt_for_model_inflection():
    prompt = build_prompt(
        "Parallel edges are allowed.",
        "en",
        "ja",
        glossary=[{"source": "parallel edge", "target": "多重辺"}],
    )
    assert "<|plamo:index|>parallel edge<|plamo:item|>多重辺" in prompt
    assert "<|plamo:index|>modular inverse" not in prompt


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


def test_custom_terms_reach_windows_pfml3_glossary():
    prompt = build_prompt(
        "An array [[PLM_0]].", "en", "ja", glossary=[{"source": "array", "target": "数列"}]
    )
    assert "<|plamo:index|>array<|plamo:item|>数列" in prompt
    assert "<|plamo:index|>array<|plamo:item|>配列" not in prompt
    assert "<|plamo:index|>[[PLM_0]]<|plamo:item|>[[PLM_0]]" in prompt


def test_canonical_terms_stay_singular_for_formatted_multiline_plural_inputs():
    terms = translation_options(
        "Find modular <plm-a>inverses</plm-a>, prefix\nsums and parallel edges.",
        "English",
        "Japanese",
    )["glossary"]
    dictionary = {item["source"]: item["target"] for item in terms}
    assert dictionary == {
        "<plm-a>": "<plm-a>",
        "</plm-a>": "</plm-a>",
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
    }


def test_canonical_defaults_are_not_injected_into_general_documents_and_can_be_overridden():
    text = "Find the modular inverse and a proper prefix."
    assert (
        translation_options(text, "English", "Japanese", document_type="general")["glossary"] == []
    )
    terms = translation_options(
        text, "English", "Japanese", glossary=[{"source": "proper prefix", "target": "真接頭辞"}]
    )["glossary"]
    assert [item for item in terms if item["source"] == "proper prefix"] == [
        {"source": "proper prefix", "target": "真接頭辞"}
    ]


def test_minimal_canonical_defaults_include_only_the_requested_corrections():
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
    }
    assert PROMPT_VERSION == "pfml3-cp-v10-glossary"
    terms = translation_options(
        "An array of integers. This is a communication problem.", "English", "Japanese"
    )["glossary"]
    assert {pair["source"]: pair["target"] for pair in terms} == TERMS
    assert len(terms) == 11


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


def test_default_glossary_is_a_fresh_copy_and_general_has_no_implicit_defaults():
    first = default_glossary()
    first[0]["target"] = "changed"
    first.append({"source": "extra", "target": "余分"})
    assert default_glossary() == [
        {"source": source, "target": target} for source, target in TERMS.items()
    ]
    assert (
        translation_options("Proper prefixes.", "en", "ja", document_type="general")["glossary"]
        == []
    )
    configured = [{"source": "prefix sum", "target": "累積和"}]
    assert (
        translation_options(
            "Some prose.", "en", "ja", document_type="general", glossary=configured
        )["glossary"]
        == configured
    )
