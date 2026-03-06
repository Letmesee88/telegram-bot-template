from __future__ import annotations
import asyncio
import json

import pytest
from sqlalchemy import select

from bot.database.models import RecommendationLogModel
from bot.services import recommender as recmod
from bot.services.recommendations_log import add_recommendation_title


@pytest.mark.asyncio
async def test_recommend_empty_returns_reason_empty(db_session, ensure_user) -> None:
    user_id = await ensure_user()

    async def _mock_openai_request(_endpoint: str, _payload: dict) -> str:
        return ""  # empty response

    # Patch function that recommender imported at module level
    orig = recmod._openai_request
    recmod._openai_request = _mock_openai_request  # type: ignore[assignment]
    try:
        rec, reason = await recmod.recommend(user_id, "bf")
        assert rec is None
        assert reason == "empty"
    finally:
        recmod._openai_request = orig  # restore


@pytest.mark.asyncio
async def test_recommend_invalid_then_retry_success_writes_log(db_session, ensure_user) -> None:
    user_id = await ensure_user()

    # First call: wrong macros (mismatch vs calories) -> triggers retry
    bad = {
        "title": "Курица и рис",
        "language": "ru",
        "nutrition": {"calories": 500, "protein_g": 10, "fat_g": 10, "carbs_g": 10},
        "portion": "1 порция",
        "why": ["баланс"],
    }
    # Second call: valid macros
    good = {
        "title": "Курица и рис",
        "language": "ru",
        "nutrition": {"calories": 480, "protein_g": 30, "fat_g": 10, "carbs_g": 50},
        "portion": "1 порция",
        "why": ["баланс"],
        "recipe_steps": ["Сварить рис", "Обжарить курицу"],
    }

    calls = {"n": 0}

    async def _mock_openai_request(_endpoint: str, _payload: dict) -> str:
        calls["n"] += 1
        return json.dumps(bad if calls["n"] == 1 else good)

    orig = recmod._openai_request
    recmod._openai_request = _mock_openai_request  # type: ignore[assignment]
    try:
        rec, reason = await recmod.recommend(user_id, "ln")
        assert reason is None
        assert rec is not None
        assert rec.get("title") == "Курица и рис"
        # Wait a moment for commit (should be immediate, but keep it robust)
        await asyncio.sleep(0.05)
        # Verify that title was persisted
        rows = (await db_session.execute(
            select(RecommendationLogModel.title).where(RecommendationLogModel.user_id == user_id).order_by(RecommendationLogModel.ts.desc()).limit(5)
        )).scalars().all()
        assert any(t.lower() == "курица и рис" for t in rows)
    finally:
        recmod._openai_request = orig


@pytest.mark.asyncio
async def test_recommend_another_uses_avoid_in_prompt(db_session, ensure_user) -> None:
    user_id = await ensure_user()
    # Pre-insert a title to be avoided
    await add_recommendation_title(db_session, user_id, "Салат Цезарь")

    captured = {"user_text": None}

    async def _mock_openai_request(_endpoint: str, payload: dict) -> str:
        # Capture the user text sent to model
        try:
            captured["user_text"] = payload["input"][0]["content"][0]["text"]
        except Exception:
            captured["user_text"] = None
        # Return a minimal valid JSON
        return json.dumps({
            "title": "Курица с овощами",
            "language": "ru",
            "nutrition": {"calories": 420, "protein_g": 35, "fat_g": 12, "carbs_g": 35},
            "portion": "1 порция",
            "why": ["разнообразие"],
        })

    orig = recmod._openai_request
    recmod._openai_request = _mock_openai_request  # type: ignore[assignment]
    try:
        rec, reason = await recmod.recommend(user_id, "dn", another=True)
        assert reason is None
        assert rec is not None
        assert captured["user_text"] is not None
        # Prompt must include the avoided title in some form (case-insensitive)
        assert "салат цезарь" in captured["user_text"].lower()
    finally:
        recmod._openai_request = orig
