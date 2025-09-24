import pytest

from bot.services.foodai import (
    _norm_detail,
    _top_components,
    _strip_code_fence,
    _lexicon_is_food_text,
)


def test_norm_detail_variants():
    assert _norm_detail("low") == "low"
    assert _norm_detail("HIGH") == "high"
    assert _norm_detail("auto") == "auto"
    assert _norm_detail("weird") == "low"  # default fallback
    assert _norm_detail(None) == "low"  # default


def test_top_components_basic_and_unique():
    items = [
        {"name": "гречка"},
        {"name": "курица"},
        {"name": "гречка"},  # duplicate, should be ignored
        {"name": "овощи"},
    ]
    res = _top_components(items, k=3)
    assert res == ["гречка", "курица", "овощи"]

    # k=2 should truncate
    res2 = _top_components(items, k=2)
    assert res2 == ["гречка", "курица"]

    # None/empty
    assert _top_components(None) == []
    assert _top_components([], k=3) == []


def test_strip_code_fence():
    s = """```json\n{\n  \"x\": 1\n}\n```"""
    assert _strip_code_fence(s) == '{\n  "x": 1\n}'

    plain = "  hello  "
    assert _strip_code_fence(plain) == "hello"

    assert _strip_code_fence(None) is None


def test_lexicon_is_food_text():
    # single-token whitelisted food/drink -> True
    assert _lexicon_is_food_text("кофе") is True
    assert _lexicon_is_food_text("чай") is True
    # single-token non-food -> None (let LLM precheck decide not_food)
    assert _lexicon_is_food_text("телефон") is None
    # multi-token phrases never short-circuit to True
    assert _lexicon_is_food_text("рыба с картошкой") is None
    # empty -> None
    assert _lexicon_is_food_text("") is None
