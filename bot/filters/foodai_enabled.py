from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram.fsm.context import FSMContext

from bot.database.models import UserModel


class FoodAIEnabledFilter(BaseFilter):
    """Allows only users with active premium AND FoodAI feature enabled.

    Conditions:
    - users.is_premium == True
    - users.foodai_enabled_at IS NOT NULL
    """

    async def __call__(self, event: Message | CallbackQuery, session: AsyncSession, state: FSMContext | None = None) -> bool:
        # Duck-typing to work with tests and wrapper objects
        user = getattr(event, "from_user", None)
        if not user:
            return False

        # If user is in ANY FSM state (e.g., onboarding), do not trigger CTA here
        try:
            if state is not None:
                cur = await state.get_state()
                if cur is not None:
                    return False
        except Exception:
            pass

        db_user = await session.get(UserModel, user.id)
        # Treat missing user as not-premium / disabled FoodAI
        is_prem = bool(getattr(db_user, "is_premium", False)) if db_user is not None else False
        has_foodai = (getattr(db_user, "foodai_enabled_at", None) is not None) if db_user is not None else False
        ok = is_prem and has_foodai
        if ok:
            return True

        # Send CTA to purchase subscription
        try:
            # We'll set dedup flags only after confirming it's a real FoodAI attempt
            msg_obj = getattr(event, "message", None)
            if msg_obj is None:
                # Duck-typing: treat event itself as message if it has text/photo
                if hasattr(event, "text") or hasattr(event, "photo"):
                    msg_obj = event

            # Do not spam during subscription navigation callbacks
            data = getattr(event, "data", None)
            if isinstance(data, str) and data.startswith("sale:"):
                return False

            # Show CTA only on actual FoodAI attempts:
            # - Photo message
            # - Plain text message (not a command)
            # - FoodAI callbacks (prefix 'foodai:')
            attempted = False
            if msg_obj is not None:
                try:
                    if getattr(msg_obj, "photo", None):
                        attempted = True
                    else:
                        t = getattr(msg_obj, "text", None)
                        if isinstance(t, str) and t.strip() and not t.startswith("/"):
                            attempted = True
                except Exception:
                    pass
            if isinstance(data, str) and data.startswith("foodai:"):
                attempted = True
            if not attempted:
                return False

            # Avoid duplicate sends across multiple handlers for the same update
            if getattr(event, "_foodai_cta_sent", False):
                return False
            setattr(event, "_foodai_cta_sent", True)
            if msg_obj is not None and msg_obj is not event:
                if getattr(msg_obj, "_foodai_cta_sent", False):
                    return False
                setattr(msg_obj, "_foodai_cta_sent", True)

            # Stop loading if it's a callback
            if hasattr(event, "answer"):
                try:
                    await event.answer()
                except Exception:
                    pass
            text = (
                "😴 Подписка не активна\n"
                "С каждым днём ты можешь быть ближе к цели, но без анализа это движение вслепую. \n"
                "calorissimo_ai_bot поможет тебе достичь нужного результата"
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="💎 Выбрать тариф", callback_data="sale:choose")]]
            )
            # Prefer replying in chat context for callbacks
            if msg_obj and hasattr(msg_obj, "answer"):
                try:
                    await msg_obj.answer(text, reply_markup=kb, disable_web_page_preview=True)
                    return False
                except Exception:
                    pass
            # Fallback: direct answer on message
            if hasattr(event, "answer"):
                try:
                    await event.answer(text, reply_markup=kb, disable_web_page_preview=True)
                except Exception:
                    pass
        except Exception:
            pass
        return False
