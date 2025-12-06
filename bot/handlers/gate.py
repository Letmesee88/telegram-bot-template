"""High-priority catch-all gate for non-onboarded users.

This router intercepts arbitrary user content (text, photo, video, sticker, animation, document)
and sends a single onboarding CTA if the user has not completed onboarding.
It must be included BEFORE other routers (e.g., FoodAI, templates) to prevent duplicates.
"""
from __future__ import annotations

from aiogram import Router, types, F
from aiogram.filters import StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.i18n import gettext as _
from sqlalchemy import select

from bot.database.database import sessionmaker
from bot.database.models import OnboardingAnswerModel

router = Router(name="gate")


def _cta_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=_("Начать"), callback_data="onboarding_start")]]
    )


async def _check_onboarding(user_id: int) -> bool:
    async with sessionmaker() as session:
        exists = await session.scalar(
            select(OnboardingAnswerModel.id).where(OnboardingAnswerModel.user_id == user_id)
        )
    return bool(exists)


@router.message(StateFilter(None), F.text & (~F.text.startswith("/")))
async def gate_text(message: types.Message) -> None:
    if not message.from_user:
        return
    if await _check_onboarding(message.from_user.id):
        return
    await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())


@router.message(StateFilter(None), F.photo)
async def gate_photo(message: types.Message) -> None:
    if not message.from_user:
        return
    if await _check_onboarding(message.from_user.id):
        return
    await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())


@router.message(StateFilter(None), F.video)
async def gate_video(message: types.Message) -> None:
    if not message.from_user:
        return
    if await _check_onboarding(message.from_user.id):
        return
    await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())


@router.message(StateFilter(None), F.sticker)
async def gate_sticker(message: types.Message) -> None:
    if not message.from_user:
        return
    if await _check_onboarding(message.from_user.id):
        return
    await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())


@router.message(StateFilter(None), F.animation)
async def gate_animation(message: types.Message) -> None:
    if not message.from_user:
        return
    if await _check_onboarding(message.from_user.id):
        return
    await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())


@router.message(StateFilter(None), F.document)
async def gate_document(message: types.Message) -> None:
    if not message.from_user:
        return
    if await _check_onboarding(message.from_user.id):
        return
    await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())


@router.message(StateFilter(None), F.voice)
async def gate_voice(message: types.Message) -> None:
    if not message.from_user:
        return
    if await _check_onboarding(message.from_user.id):
        return
    await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())


@router.message(StateFilter(None), F.video_note)
async def gate_video_note(message: types.Message) -> None:
    if not message.from_user:
        return
    if await _check_onboarding(message.from_user.id):
        return
    await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())
