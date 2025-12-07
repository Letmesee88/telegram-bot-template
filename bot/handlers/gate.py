"""High-priority catch-all gate for non-onboarded users.

This router intercepts arbitrary user content (text, photo, video, sticker, animation, document)
and sends a single onboarding CTA if the user has not completed onboarding.
It must be included BEFORE other routers (e.g., FoodAI, templates) to prevent duplicates.

IMPORTANT: Handlers here only trigger for NON-onboarded users (via OnboardingGateFilter).
Onboarded users pass through to subsequent routers (FoodAI, etc).
"""
from __future__ import annotations

from aiogram import Router, types, F
from aiogram.filters import BaseFilter, StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.i18n import gettext as _
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import OnboardingAnswerModel

router = Router(name="gate")


class OnboardingGateFilter(BaseFilter):
    """Filter that passes only if user has NOT completed onboarding.
    
    If onboarding exists -> returns False (let other routers handle).
    If no onboarding -> returns True (this handler will show CTA).
    """
    async def __call__(self, event: types.Message, session: AsyncSession) -> bool:
        user = getattr(event, "from_user", None)
        if not user:
            return False
        exists = await session.scalar(
            select(OnboardingAnswerModel.id).where(OnboardingAnswerModel.user_id == user.id)
        )
        # Return True only if NOT onboarded (to show CTA)
        return not bool(exists)


def _cta_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=_("Начать"), callback_data="onboarding_start")]]
    )


@router.message(StateFilter(None), OnboardingGateFilter(), F.text & (~F.text.startswith("/")))
async def gate_text(message: types.Message) -> None:
    await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())


@router.message(StateFilter(None), OnboardingGateFilter(), F.photo)
async def gate_photo(message: types.Message) -> None:
    await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())


@router.message(StateFilter(None), OnboardingGateFilter(), F.video)
async def gate_video(message: types.Message) -> None:
    await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())


@router.message(StateFilter(None), OnboardingGateFilter(), F.sticker)
async def gate_sticker(message: types.Message) -> None:
    await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())


@router.message(StateFilter(None), OnboardingGateFilter(), F.animation)
async def gate_animation(message: types.Message) -> None:
    await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())


@router.message(StateFilter(None), OnboardingGateFilter(), F.document)
async def gate_document(message: types.Message) -> None:
    await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())


@router.message(StateFilter(None), OnboardingGateFilter(), F.voice)
async def gate_voice(message: types.Message) -> None:
    await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())


@router.message(StateFilter(None), OnboardingGateFilter(), F.video_note)
async def gate_video_note(message: types.Message) -> None:
    await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())
