from __future__ import annotations

import pytest

from bot.services.recommender import _is_nutrition_valid


@pytest.mark.parametrize(
    "data, target_cal_max, enforce_cap, expected",
    [
        (
            {"nutrition": {"calories": 400, "protein_g": 30, "fat_g": 15, "carbs_g": 35}},
            None,
            False,
            True,
        ),
        (
            # mismatch: est=170 vs cal=500 -> invalid
            {"nutrition": {"calories": 500, "protein_g": 10, "fat_g": 10, "carbs_g": 10}},
            None,
            False,
            False,
        ),
        (
            # enforce_cap off -> still valid even if above cap
            {"nutrition": {"calories": 460, "protein_g": 30, "fat_g": 20, "carbs_g": 35}},
            400,
            False,
            True,
        ),
        (
            # enforce_cap on -> invalid if > target*1.10 (440)
            {"nutrition": {"calories": 460, "protein_g": 30, "fat_g": 10, "carbs_g": 30}},
            400,
            True,
            False,
        ),
    ],
)
def test_is_nutrition_valid(data, target_cal_max, enforce_cap, expected):
    ok, _ = _is_nutrition_valid(data, target_cal_max, enforce_cap)
    assert ok is expected
