#!/usr/bin/env python3
from __future__ import annotations

"""
Smoke tests for FoodAI photo escalation logic.

Usage examples:
  - python scripts/smoke_foodai.py --scenario low_conf
  - python scripts/smoke_foodai.py --scenario many_items
  - python scripts/smoke_foodai.py --scenario provider_error

The script monkeypatches Telegram file URL resolution and OpenAI calls to avoid
real network requests. It prints the final JSON result of analyze_photo().
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Ensure project root is importable
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.services import foodai

if TYPE_CHECKING:
    from collections.abc import Callable


def _json_result(
    *,
    title: str = "Тестовое блюдо",
    calories: int = 450,
    protein_g: float = 28.0,
    fat_g: float = 15.0,
    carbs_g: float = 45.0,
    weight_g: float = 350.0,
    confidence: float = 0.85,
    items_count: int = 2,
) -> str:
    items = [
        {
            "name": f"Ингредиент {i+1}",
            "calories": calories // max(1, items_count),
            "protein_g": round(protein_g / max(1, items_count), 1),
            "fat_g": round(fat_g / max(1, items_count), 1),
            "carbs_g": round(carbs_g / max(1, items_count), 1),
            "weight_g": round(weight_g / max(1, items_count), 1),
        }
        for i in range(items_count)
    ]
    doc = {
        "title": title,
        "calories": calories,
        "protein_g": protein_g,
        "fat_g": fat_g,
        "carbs_g": carbs_g,
        "weight_g": weight_g,
        "confidence": confidence,
        "items": items,
        "references": {
            "sources": [
                'ФГБУН "ФИЦ питания и биотехнологии"',
                "USDA FoodData Central",
            ]
        },
        "analysis_text": "Тестовый анализ блюда. Использованы справочные данные ФИЦ питания и USDA.",
        "appearance": {"is_packaged": False, "plate_visible": True, "plate_diameter_cm": 24},
        "not_food": False,
    }
    return json.dumps(doc, ensure_ascii=False)


def make_stub_openai_request(sequence: list[str | None]) -> Callable[[str, dict[str, Any]], Any]:
    idx = {"i": 0}

    async def _stub(kind: str, payload: dict[str, Any]) -> str | None:
        # Return next response in sequence; None simulates provider error.
        i = idx["i"]
        if i >= len(sequence):
            return sequence[-1] if sequence else None
        idx["i"] = i + 1
        return sequence[i]

    return _stub


async def run_scenario(scn: str) -> None:
    # Monkeypatch Telegram file URL + precheck
    async def _stub_tg_file_url(file_id: str) -> str | None:
        return "https://example.com/test.jpg"

    async def _stub_foodness(file_url: str) -> bool | None:
        return True

    # Build sequence by scenario
    if scn == "low_conf":
        # First attempt: low confidence -> triggers escalation; second attempt: strong
        seq = [
            _json_result(confidence=0.5, items_count=2),
            _json_result(confidence=0.88, items_count=2),
        ]
    elif scn == "many_items":
        # First attempt: many items with borderline confidence -> escalate; then strong
        seq = [
            _json_result(confidence=0.72, items_count=4),
            _json_result(confidence=0.9, items_count=3),
        ]
    elif scn == "provider_error":
        # First attempt: provider returns None -> next step; then strong
        seq = [
            None,
            _json_result(confidence=0.86, items_count=2),
        ]
    else:
        msg = f"Unknown scenario: {scn}"
        raise SystemExit(msg)

    # Apply monkeypatches
    orig_tg = foodai._tg_file_url
    orig_foodness = foodai._foodness_photo if hasattr(foodai, "_foodness_photo") else None
    orig_oa = foodai._openai_request
    try:
        foodai._tg_file_url = _stub_tg_file_url  # type: ignore
        if orig_foodness is not None:
            foodai._foodness_photo = _stub_foodness  # type: ignore
        foodai._openai_request = make_stub_openai_request(seq)  # type: ignore

        await foodai.analyze_photo("fake_file_id")
    finally:
        # Restore originals
        foodai._tg_file_url = orig_tg  # type: ignore
        if orig_foodness is not None:
            foodai._foodness_photo = orig_foodness  # type: ignore
        foodai._openai_request = orig_oa  # type: ignore


async def main() -> None:
    parser = argparse.ArgumentParser(description="FoodAI vision escalation smoke tests")
    parser.add_argument("--scenario", choices=["low_conf", "many_items", "provider_error"], required=True)
    args = parser.parse_args()

    await run_scenario(args.scenario)


if __name__ == "__main__":
    asyncio.run(main())
