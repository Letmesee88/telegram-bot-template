from __future__ import annotations

from typing import Any


async def analyze_photo(file_id: str) -> dict[str, Any]:
    """Stub: analyze a photo and return macro nutrients estimation.

    In production, integrate a real vision model. For now returns fixed-ish values.
    """
    # Fake deterministic output based on file_id hash length just to vary a little
    base = (len(file_id) % 100) + 250
    protein = round(base * 0.25 / 4, 1)  # grams assuming 4 kcal/g
    fat = round(base * 0.30 / 9, 1)      # grams assuming 9 kcal/g
    carbs = round(base * 0.45 / 4, 1)    # grams assuming 4 kcal/g
    weight = round(protein * 4 + fat * 9 + carbs * 4, 1)  # pseudo-weight proxy

    return {
        "calories": int(base),
        "protein_g": float(protein),
        "fat_g": float(fat),
        "carbs_g": float(carbs),
        "weight_g": float(weight),
        "confidence": 0.72,
        "items": [
            {"name": "Блюдо", "calories": int(base), "protein_g": float(protein), "fat_g": float(fat), "carbs_g": float(carbs)},
        ],
        "references": {
            "sources": [
                "ФГБУН \"ФИЦ питания и биотехнологии\"",
                "USDA FoodData Central",
            ]
        },
    }


async def analyze_text(text: str) -> dict[str, Any]:
    """Stub: analyze a text description and return macro estimation."""
    words = len(text.split())
    base = 150 + (words % 200)
    protein = round(base * 0.2 / 4, 1)
    fat = round(base * 0.3 / 9, 1)
    carbs = round(base * 0.5 / 4, 1)
    weight = round(protein * 4 + fat * 9 + carbs * 4, 1)

    return {
        "calories": int(base),
        "protein_g": float(protein),
        "fat_g": float(fat),
        "carbs_g": float(carbs),
        "weight_g": float(weight),
        "confidence": 0.65,
        "items": [
            {"name": "Описание", "calories": int(base), "protein_g": float(protein), "fat_g": float(fat), "carbs_g": float(carbs)},
        ],
        "references": {
            "sources": [
                "ФГБУН \"ФИЦ питания и биотехнологии\"",
                "USDA FoodData Central",
            ]
        },
    }
