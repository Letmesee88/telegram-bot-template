from __future__ import annotations

import re
import time
from typing import Any

from aiogram import F, Router, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.core.config import settings
from bot.services.analytics import analytics
from bot.analytics.types import BaseEvent, EventProperties, Plan
from bot.services.recommender import recommend


router = Router()


def _rec_enabled() -> bool:
    try:
        return bool(getattr(settings, "RECOMMENDATIONS_ENABLED", True))
    except Exception:
        return True


def _rec_type_kb(ctx: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🥞 Завтрак", callback_data=f"rec:type:bf:{ctx}"),
                InlineKeyboardButton(text="🍜 Обед", callback_data=f"rec:type:ln:{ctx}"),
            ],
            [
                InlineKeyboardButton(text="🥗 Ужин", callback_data=f"rec:type:dn:{ctx}"),
                InlineKeyboardButton(text="🍎 Перекус", callback_data=f"rec:type:snack:{ctx}"),
            ],
        ]
    )


def _other_kb(meal_type: str, ctx: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🎲 Хочу другой вариант", callback_data=f"rec:other:{meal_type}:{ctx}")]]
    )


def _human_type(meal_type: str) -> str:
    return {"bf": "завтрак", "ln": "обед", "dn": "ужин", "snack": "перекус"}.get(meal_type, "приём пищи")


def _build_recommendation_text(rec: dict[str, Any], meal_code: str | None = None) -> str:
    title = str(rec.get("title") or "Блюдо").strip()
    descr = str(rec.get("description") or "").strip()
    nutr = rec.get("nutrition") or {}
    cal = int(float(nutr.get("calories") or 0))
    p = float(nutr.get("protein_g") or 0)
    f = float(nutr.get("fat_g") or 0)
    c = float(nutr.get("carbs_g") or 0)
    portion = str(rec.get("portion") or "").strip()
    why = [str(x).strip() for x in (rec.get("why") or []) if str(x).strip()]
    cook_min = int(float(rec.get("cook_time_min") or 0))
    diff = str(rec.get("difficulty") or "Легко").strip()
    steps = [str(x).strip() for x in (rec.get("recipe_steps") or []) if str(x).strip()]
    tips = [str(x).strip() for x in (rec.get("tips") or []) if str(x).strip()]
    mtype = str(rec.get("meal_type") or "").strip().lower()
    # Prefer explicit code from callback to avoid RU label mismatch
    if meal_code in {"bf", "ln", "dn", "snack"}:
        mtype = meal_code

    parts: list[str] = []
    parts.append(
        f"🍽 Рекомендую на {_human_type({'bf': 'bf', 'ln': 'ln', 'dn': 'dn', 'snack': 'snack'}.get(mtype, mtype))}: {title}"
    )
    if descr:
        parts.append("")
        parts.append(descr)
    parts.append("")
    parts.append("📊 Питательная ценность:")
    parts.append(f"🔥 {cal} ккал")
    parts.append(f"🥩 {p:g}г белка")
    parts.append(f"🥑 {f:g}г жиров")
    parts.append(f"🍞 {c:g}г углеводов")
    if portion:
        parts.append(f"⚪️ Порция: {portion}")
    if why:
        parts.append("")
        parts.append("Почему именно это:")
        for w in why[:4]:
            parts.append(f"✓ {w}")
    if cook_min:
        parts.append("")
        parts.append(f"⏰ Время готовки: {cook_min} мин • 👍 {diff}")
    if steps:
        parts.append("")
        parts.append("📝 Рецепт:")
        for i, s in enumerate(steps[:6], start=1):
            parts.append(f"{i}. {s}")
    if tips:
        parts.append("")
        parts.append("💡 Советы:")
        for t in tips[:3]:
            parts.append(f"• {t}")
    return "\n".join(parts)


@router.callback_query(F.data.regexp(r"^rec:start:(\d+)$"))
async def cb_rec_start(callback: types.CallbackQuery) -> None:
    if not _rec_enabled():
        await callback.answer()
        return
    m = re.match(r"^rec:start:(\d+)$", callback.data or "")
    if not m or not callback.from_user:
        await callback.answer()
        return
    meal_id = int(m.group(1))
    user_id = callback.from_user.id
    ctx = f"{user_id}:{int(time.time())}:{meal_id}"

    if analytics.logger:
        analytics.fire_event(
            BaseEvent(
                user_id=user_id,
                event_type="Rec:StartClicked",
                event_properties=EventProperties(
                    chat_id=callback.message.chat.id if callback.message else None,
                    chat_type=callback.message.chat.type if callback.message else None,
                    text=f"meal_id={meal_id}",
                ),
                language=getattr(callback.from_user, 'language_code', None),
                plan=Plan(branch="Recommend", source="FoodAI", version="v1"),
            )
        )

    try:
        await callback.message.answer(
            "Какое блюдо хочешь получить в рекомендации?",
            reply_markup=_rec_type_kb(ctx),
        )
    finally:
        await callback.answer()


@router.callback_query(F.data.regexp(r"^rec:type:(bf|ln|dn|snack):(.+)$"))
async def cb_rec_type(callback: types.CallbackQuery) -> None:
    if not _rec_enabled():
        await callback.answer()
        return
    m = re.match(r"^rec:type:(bf|ln|dn|snack):(.+)$", callback.data or "")
    if not m or not callback.from_user:
        await callback.answer()
        return
    meal_type = m.group(1)
    ctx = m.group(2)
    user_id = callback.from_user.id

    if analytics.logger:
        analytics.fire_event(
            BaseEvent(
                user_id=user_id,
                event_type="Rec:TypeChosen",
                event_properties=EventProperties(
                    chat_id=callback.message.chat.id if callback.message else None,
                    chat_type=callback.message.chat.type if callback.message else None,
                    text=f"type={meal_type}",
                ),
                language=getattr(callback.from_user, 'language_code', None),
                plan=Plan(branch="Recommend", source="FoodAI", version="v1"),
            )
        )

    try:
        rec = await recommend(user_id, meal_type)  # type: ignore[arg-type]
        if not rec:
            await callback.message.answer("Не удалось подготовить рекомендацию. Попробуйте ещё раз.")
            await callback.answer()
            return
        text = _build_recommendation_text(rec, meal_type)
        await callback.message.answer(text, reply_markup=_other_kb(meal_type, ctx))
        # Analytics: Rec:Generated
        try:
            if analytics.logger:
                title = str(rec.get("title") or "").strip()
                cal = (rec.get("nutrition") or {}).get("calories")
                analytics.fire_event(
                    BaseEvent(
                        user_id=user_id,
                        event_type="Rec:Generated",
                        event_properties=EventProperties(
                            chat_id=callback.message.chat.id if callback.message else None,
                            chat_type=callback.message.chat.type if callback.message else None,
                            text=f"type={meal_type}; title={title}; cal={cal}",
                        ),
                        language=getattr(callback.from_user, 'language_code', None),
                        plan=Plan(branch="Recommend", source="FoodAI", version="v1"),
                    )
                )
        except Exception:
            pass
    finally:
        await callback.answer()


@router.callback_query(F.data.regexp(r"^rec:other:(bf|ln|dn|snack):(.+)$"))
async def cb_rec_other(callback: types.CallbackQuery) -> None:
    if not _rec_enabled():
        await callback.answer()
        return
    m = re.match(r"^rec:other:(bf|ln|dn|snack):(.+)$", callback.data or "")
    if not m or not callback.from_user:
        await callback.answer()
        return
    meal_type = m.group(1)
    ctx = m.group(2)
    user_id = callback.from_user.id

    if analytics.logger:
        analytics.fire_event(
            BaseEvent(
                user_id=user_id,
                event_type="Rec:OtherClicked",
                event_properties=EventProperties(
                    chat_id=callback.message.chat.id if callback.message else None,
                    chat_type=callback.message.chat.type if callback.message else None,
                    text=f"type={meal_type}",
                ),
                language=getattr(callback.from_user, 'language_code', None),
                plan=Plan(branch="Recommend", source="FoodAI", version="v1"),
            )
        )

    try:
        rec = await recommend(user_id, meal_type, another=True)  # type: ignore[arg-type]
        if not rec:
            await callback.message.answer("Не удалось подготовить рекомендацию. Попробуйте ещё раз.")
            await callback.answer()
            return
        text = _build_recommendation_text(rec, meal_type)
        await callback.message.answer(text, reply_markup=_other_kb(meal_type, ctx))
        # Analytics: Rec:Generated (other)
        try:
            if analytics.logger:
                title = str(rec.get("title") or "").strip()
                cal = (rec.get("nutrition") or {}).get("calories")
                analytics.fire_event(
                    BaseEvent(
                        user_id=user_id,
                        event_type="Rec:Generated",
                        event_properties=EventProperties(
                            chat_id=callback.message.chat.id if callback.message else None,
                            chat_type=callback.message.chat.type if callback.message else None,
                            text=f"type={meal_type}; other=1; title={title}; cal={cal}",
                        ),
                        language=getattr(callback.from_user, 'language_code', None),
                        plan=Plan(branch="Recommend", source="FoodAI", version="v1"),
                    )
                )
        except Exception:
            pass
    finally:
        await callback.answer()
