"""The model wire grammar cannot lose or manufacture browser DOM slots."""

import pytest

from plamo_cp.engine import check_placeholders, translation_options
from plamo_cp.marker_codec import MarkerCodec, prepare_model_input


def test_exact_codeforces_sentences_use_readable_span_and_bijective_math_aliases():
    source = "The subsequence of <plm-a>all</plm-a> round brackets forms a correct bracket sequence[[PLM_9]];"
    codec = MarkerCodec(source)
    assert (
        codec.encode()
        == 'The subsequence of <span id="plm-a">all</span> round brackets forms a correct bracket sequence<math-a/>;'
    )
    result = codec.decode(
        '<span id="plm-a">すべて</span>の丸括弧からなる部分列は正しい括弧列<math-a/>となる。'
    )
    assert result == "<plm-a>すべて</plm-a>の丸括弧からなる部分列は正しい括弧列[[PLM_9]]となる。"
    check_placeholders(source, result)


def test_output_sentence_can_reorder_all_four_math_slots_without_duplicating_k():
    source = "For each test case, output the rounds for [[PLM_0]] to equal [[PLM_1]] after exactly [[PLM_2]] changes to [[PLM_3]]."
    codec = MarkerCodec(source)
    result = codec.decode(
        "<math-d/>にちょうど<math-c/>回の変更を加えた後、<math-a/>が<math-b/>になるラウンド数。"
    )
    assert (
        result
        == "[[PLM_3]]にちょうど[[PLM_2]]回の変更を加えた後、[[PLM_0]]が[[PLM_1]]になるラウンド数。"
    )
    check_placeholders(source, result)


def test_nested_multiple_spans_decode_by_identity_and_a_stack():
    source = "<plm-a>first <plm-z>inner [[PLM_8]]</plm-z></plm-a> then <plm-aa>last</plm-aa>"
    codec = MarkerCodec(source)
    encoded = codec.encode()
    assert codec.decode(encoded) == source
    moved = '<span id="plm-aa">最後</span>の前は<span id="plm-a">最初<span id="plm-z"><math-a/>内側</span></span>'
    check_placeholders(source, codec.decode(moved))


@pytest.mark.parametrize(
    "result",
    [
        '<span id="plm-a">本文</span>',
        '<span id="plm-a">本文</span><math-a/><math-a/>',
        '<span id="plm-a">本文</span><math-z/>',
        '<span id="plm-z">本文</span><math-a/>',
        '<span id="plm-a">本文<math-a/>',
        '</span><span id="plm-a">本文<math-a/>',
        '<span id="plm-a" onclick="bad()">本文</span><math-a/>',
        "<span>本文</span><math-a/>",
        '<span id="plm-a">本文</span><math-a>',
        '<span id="plm-a">本文</span><math-a/></math-a>',
        '<span id="plm-a">本文</span>[[PLM_0]]',
        "<plm-a>本文</plm-a><math-a/>",
        '<span id="plm-a">本文</span><math-a/><math-',
    ],
)
def test_missing_unknown_duplicate_mixed_or_malformed_aliases_are_rejected(result):
    with pytest.raises(ValueError, match="識別子"):
        MarkerCodec("<plm-a>prose</plm-a> [[PLM_0]]").decode(result)


@pytest.mark.parametrize(
    "tail", ["<", "<m", "<math-", "<math-a", "<math-a/", "<sp", '<span id="plm-a"']
)
def test_streamed_incomplete_aliases_are_withheld_until_known(tail):
    codec = MarkerCodec("<plm-a>prose</plm-a> [[PLM_0]]")
    assert codec.decode("訳文" + tail, partial=True) == "訳文"


def test_stream_prefixes_restore_canonical_known_tags_but_never_accept_duplicate_slots():
    codec = MarkerCodec("<plm-a>prose</plm-a> [[PLM_0]]")
    assert codec.decode('<span id="plm-a">訳文<math-a/>', partial=True) == "<plm-a>訳文[[PLM_0]]"
    assert codec.decode('<span id="plm-a">訳文</sp', partial=True) == "<plm-a>訳文"
    with pytest.raises(ValueError):
        codec.decode("<math-a/><math-a/>", partial=True)
    with pytest.raises(ValueError):
        codec.decode('<span id="plm-a">訳文', partial=False)


def test_identity_plain_text_and_literal_wire_collisions_use_legacy_without_interpretation():
    assert MarkerCodec("Plain text.").decode("普通の文章。") == "普通の文章。"
    source = 'Explain literal <span id="plm-a">text</span> and [[PLM_0]].'
    prepared = prepare_model_input(source, translation_options(source, "en", "ja"))
    assert not prepared.codec.enabled and prepared.text == source
    assert prepared.codec.decode(source) == source
    assert "Tokens of the form [[PLM_0]]" in prepared.options["context"]


def test_glossary_and_history_collisions_keep_one_consistent_legacy_prompt():
    source = "Current [[PLM_0]]."
    history = [{"source": "Literal <math-a/>", "translation": "記法<math-a/>"}]
    prepared = prepare_model_input(source, translation_options(source, "en", "ja"), history)
    assert not prepared.codec.enabled and prepared.history == history
    assert prepared.text == source
    terms = [{"source": "<span>", "target": "タグ"}]
    prepared = prepare_model_input(source, translation_options(source, "en", "ja", glossary=terms))
    assert not prepared.codec.enabled and prepared.options["glossary"][-1] == terms[0]


def test_only_internal_format_identities_are_removed_and_explicit_dictionary_stays_authoritative():
    source = "<plm-a>all</plm-a> [[PLM_7]]"
    for terms in ([], [{"source": "all", "target": "すべて"}]):
        options = translation_options(source, "en", "ja", glossary=terms)
        prepared = prepare_model_input(source, options)
        assert prepared.options["glossary"] == [
            *terms,
            {"source": "<math-a/>", "target": "<math-a/>"},
        ]
        assert options["glossary"][0] == {"source": "<plm-a>", "target": "<plm-a>"}


def test_past_pairs_are_encoded_with_their_own_mapping_without_mutation():
    history = [
        {
            "source": "<plm-b>Earlier</plm-b> [[PLM_19]].",
            "translation": "<plm-b>前</plm-b>[[PLM_19]]。",
        }
    ]
    source = "Current [[PLM_2]]."
    prepared = prepare_model_input(source, translation_options(source, "en", "ja"), history)
    assert prepared.history == [
        {
            "source": '<span id="plm-b">Earlier</span> <math-a/>.',
            "translation": '<span id="plm-b">前</span><math-a/>。',
        }
    ]
    assert "[[PLM_19]]" in history[0]["source"]
    assert prepared.codec.decode("現在<math-a/>。") == "現在[[PLM_2]]。"


def test_plain_current_input_cannot_copy_wire_aliases_from_formatted_history():
    history = [{"source": "Earlier [[PLM_0]].", "translation": "前[[PLM_0]]。"}]
    prepared = prepare_model_input(
        "Plain current input.", translation_options("Plain current input.", "en", "ja"), history
    )
    assert prepared.text == "Plain current input."
    assert prepared.codec.decode("Ordinary <comparison text.") == "Ordinary <comparison text."
    for leaked in ("Copied <math-a/>", '<span id="plm-a">Copied</span>'):
        with pytest.raises(ValueError):
            prepared.codec.decode(leaked)


def test_literal_wire_in_legacy_source_context_keeps_the_entire_prompt_consistent():
    source = "Current [[PLM_9]]."
    context = 'A reference mentions literal <math-a/> and <span id="plm-a">tags</span>.'
    prepared = prepare_model_input(
        source, translation_options(source, "en", "ja", source_context=context)
    )
    assert not prepared.codec.enabled and prepared.codec.legacy
    assert prepared.text == source
    assert context in prepared.options["context"]
    assert "Tokens of the form [[PLM_0]]" in prepared.options["context"]


@pytest.mark.parametrize(
    "source", ["<plm-a>unclosed", "</plm-a>before<plm-a>", "<plm-a><plm-b>crossed</plm-a></plm-b>"]
)
def test_invalid_original_formatting_is_rejected_before_model_work(source):
    with pytest.raises(ValueError):
        MarkerCodec(source).encode()


def test_many_aliases_are_deterministic_and_do_not_collide_after_z():
    source = " ".join(f"[[PLM_{index + 100}]]" for index in range(28))
    codec = MarkerCodec(source)
    assert "<math-z/> <math-aa/> <math-ab/>" in codec.encode()
    assert codec.decode(codec.encode()) == source


@pytest.mark.parametrize("suffix", ["<", "<ma", "</sp", '<span id="', "<math-"])
def test_complete_alias_counts_do_not_authorize_a_trailing_incomplete_wire_tag(suffix):
    codec = MarkerCodec("Original [[PLM_0]].")
    assert codec.decode("<math-a/>訳文" + suffix, partial=True) == "[[PLM_0]]訳文"
    with pytest.raises(ValueError):
        codec.decode("<math-a/>訳文" + suffix)
    assert MarkerCodec("Plain source.").decode("途中訳" + suffix, partial=True) == "途中訳"


def test_literal_less_than_comparison_does_not_consume_the_following_math_alias():
    codec = MarkerCodec("Make a value < [[PLM_0]].")
    assert codec.encode() == "Make a value < <math-a/>."
    assert codec.decode("値 < <math-a/>。") == "値 < [[PLM_0]]。"
    assert codec.decode("値 < <math-a/>", partial=True) == "値 < [[PLM_0]]"


@pytest.mark.parametrize("prefix", ["<math-<x>", '<span id="<x>', "</span<x>"])
@pytest.mark.parametrize("partial", [False, True])
def test_malformed_reserved_literal_before_a_known_alias_cannot_escape_validation(prefix, partial):
    codec = MarkerCodec("Original [[PLM_0]].")
    with pytest.raises(ValueError):
        codec.decode(prefix + "other text <math-a/>", partial=partial)


def test_long_literal_angle_prefix_is_preserved_without_swallowing_a_known_alias():
    prefix = "literal < " * 10000
    codec = MarkerCodec(prefix + "[[PLM_89]].")
    assert codec.decode(prefix + "<math-a/>。") == prefix + "[[PLM_89]]。"
