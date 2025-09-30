import asyncio
import sys
import os
from typing import Dict, Any

# Make repo root importable when running as: python tests/manual_foodai_edit.py
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_THIS_DIR)
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from bot.services import foodai


def _make_base() -> Dict[str, Any]:
    # Base meal approximates the screenshot: pasta + chicken + tomatoes + croutons
    return {
        "title": "Паста с курицей, вялеными помидорами и сухариками",
        "calories": 720,
        "protein_g": 22.2,
        "fat_g": 24.0,
        "carbs_g": 102.7,
        "weight_g": 400.0,
        "items": [
            {"name": "паста (фузилли)", "weight_g": 200.0, "calories": 316, "protein_g": 11.6, "fat_g": 1.8, "carbs_g": 60.0},
            {"name": "куриное филе (запечённое)", "weight_g": 150.0, "calories": 248, "protein_g": 46.5, "fat_g": 3.9, "carbs_g": 0.0},
            {"name": "вяленые черри-помидоры", "weight_g": 30.0, "calories": 30, "protein_g": 1.0, "fat_g": 0.2, "carbs_g": 6.5},
            {"name": "сухарики/панировочные крошки", "weight_g": 40.0, "calories": 130, "protein_g": 3.0, "fat_g": 2.5, "carbs_g": 24.0},
        ],
        "source": "test",
    }


async def _safe_foodness(text: str | None) -> bool | None:
    t = (text or "").lower()
    if "телефон" in t or "ноутбук" in t:
        return False
    # For tests, assume food otherwise (avoid external API)
    return True


def _install_patches():
    # Avoid any OpenAI calls in tests
    foodai._use_openai = lambda: False  # type: ignore
    # Monkeypatch foodness classifier to local stub
    foodai._foodness_text = _safe_foodness  # type: ignore


async def _call(instr: str) -> Dict[str, Any]:
    base = _make_base()
    return await foodai.refine_meal(base, instr)


def _find_item(items, needle: str):
    n = needle.lower()
    for it in items:
        if n in str(it.get("name") or "").lower():
            return it
    return None


async def test_add_parsley_40g():
    r = await _call("добавь петрушку 40 г")
    assert not r.get("error"), f"unexpected error: {r}"
    it = _find_item(r.get("items") or [], "петруш")
    assert it, "no parsley item"
    w = float(it.get("weight_g") or 0)
    assert 39.0 <= w <= 41.0, f"expected ~40g, got {w}"


async def test_add_basil_default():
    r = await _call("добавь базилик")
    assert not r.get("error"), f"unexpected error: {r}"
    it = _find_item(r.get("items") or [], "базилик")
    assert it, "no basil item"
    w = float(it.get("weight_g") or 0)
    assert 4.0 <= w <= 6.5, f"expected ~5g, got {w}"


async def test_add_greens_default():
    r = await _call("добавь зелень")
    assert not r.get("error"), f"unexpected error: {r}"
    it = _find_item(r.get("items") or [], "зелень")
    assert it, "no greens item"
    w = float(it.get("weight_g") or 0)
    assert 4.0 <= w <= 6.5, f"expected ~5g, got {w}"


async def test_add_sauce_default():
    r = await _call("добавь соус")
    assert not r.get("error"), f"unexpected error: {r}"
    it = _find_item(r.get("items") or [], "соус")
    assert it, "no sauce item"
    w = float(it.get("weight_g") or 0)
    assert 18.0 <= w <= 22.5, f"expected ~20g, got {w}"


async def test_replace_chicken_to_fish():
    r = await _call("замени курицу на рыбу")
    assert not r.get("error"), f"unexpected error: {r}"
    items = r.get("items") or []
    assert _find_item(items, "рыб"), "fish not found after replace"
    assert not _find_item(items, "курин"), "chicken still present"
    it = _find_item(items, "рыб")
    w = float(it.get("weight_g") or 0)
    assert 140.0 <= w <= 160.0, f"fish weight should inherit ~150g, got {w}"


async def test_replace_croutons_to_bread():
    r = await _call("замени сухарики на хлеб")
    assert not r.get("error"), f"unexpected error: {r}"
    items = r.get("items") or []
    assert _find_item(items, "хлеб"), "bread not found after replace"
    assert not _find_item(items, "сухар"), "croutons still present"


async def test_change_qty_pasta():
    r = await _call("паста 300 г")
    assert not r.get("error"), f"unexpected error: {r}"
    it = _find_item(r.get("items") or [], "паста")
    assert it, "no pasta item"
    w = float(it.get("weight_g") or 0)
    assert 295.0 <= w <= 305.0, f"expected 300g pasta, got {w}"


async def test_not_food_rejected():
    r = await _call("добавь телефон 1 г")
    assert r.get("error") == "not_food", f"expected not_food error, got {r}"


async def main():
    _install_patches()
    tests = [
        test_add_parsley_40g,
        test_add_basil_default,
        test_add_greens_default,
        test_add_sauce_default,
        test_replace_chicken_to_fish,
        test_replace_croutons_to_bread,
        test_change_qty_pasta,
        test_not_food_rejected,
    ]
    failed = 0
    for t in tests:
        name = t.__name__
        try:
            await t()
            print(f"[OK] {name}")
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"[ERROR] {name}: {e}")
    if failed:
        print(f"\nFAILED: {failed} test(s)")
        sys.exit(1)
    print("\nAll tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
