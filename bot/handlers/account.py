from __future__ import annotations

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.utils.i18n import gettext as _

from bot.services.account import get_account_summary_text
from bot.services.analytics import analytics
from bot.analytics.types import BaseEvent, EventProperties

router = Router(name="account")


def _kb_account() -> types.InlineKeyboardMarkup:
    rows: list[list[types.InlineKeyboardButton]] = []
    rows.append([types.InlineKeyboardButton(text=_("⚖️ Мой вес"), callback_data="weight:open:account")])
    rows.append([types.InlineKeyboardButton(text=_("⚙️ Настройки"), callback_data="settings:open")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("account"))
async def cmd_account(message: types.Message) -> None:
    if not message.from_user:
        return
    user_id = message.from_user.id

    text = await get_account_summary_text(user_id)
    await message.answer(text, reply_markup=_kb_account())

    if analytics.logger and message.from_user:
        try:
            analytics.fire_event(
                BaseEvent(
                    user_id=message.from_user.id,
                    event_type="AccountOpened",
                    event_properties=EventProperties(
                        chat_id=message.chat.id if message.chat else None,
                        chat_type=message.chat.type if message.chat else None,
                        command="/account",
                    ),
                    language=message.from_user.language_code if message.from_user else None,
                )
            )
        except Exception:
            pass


@router.callback_query(F.data.regexp(r"^account:open(?::(today|history|weight))?$"))
async def cb_account_open(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    source = None
    try:
        parts = (callback.data or "").split(":")
        if len(parts) >= 3:
            source = parts[2]
    except Exception:
        source = None

    text = await get_account_summary_text(user_id)
    try:
        await callback.message.answer(text, reply_markup=_kb_account())
    except Exception:
        await callback.answer(_("Личный кабинет"), show_alert=False)

    # Analytics
    if analytics.logger and callback.from_user:
        try:
            # Generic open
            analytics.fire_event(
                BaseEvent(
                    user_id=callback.from_user.id,
                    event_type="AccountOpened",
                    event_properties=EventProperties(
                        chat_id=callback.message.chat.id if callback.message else None,
                        chat_type=callback.message.chat.type if callback.message else None,
                        command=None,
                        text=f"source={source or 'button'}",
                    ),
                    language=getattr(callback.from_user, 'language_code', None),
                )
            )
            # Source-specific
            if source == "today":
                analytics.fire_event(
                    BaseEvent(
                        user_id=callback.from_user.id,
                        event_type="AccountButtonFromToday",
                        event_properties=EventProperties(
                            chat_id=callback.message.chat.id if callback.message else None,
                            chat_type=callback.message.chat.type if callback.message else None,
                        ),
                        language=getattr(callback.from_user, 'language_code', None),
                    )
                )
            elif source == "history":
                analytics.fire_event(
                    BaseEvent(
                        user_id=callback.from_user.id,
                        event_type="AccountButtonFromHistory",
                        event_properties=EventProperties(
                            chat_id=callback.message.chat.id if callback.message else None,
                            chat_type=callback.message.chat.type if callback.message else None,
                        ),
                        language=getattr(callback.from_user, 'language_code', None),
                    )
                )
        except Exception:
            pass
