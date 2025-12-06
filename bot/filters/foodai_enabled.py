from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram.fsm.context import FSMContext
from sqlalchemy import update, func, select

from bot.database.models import UserModel, OnboardingAnswerModel
from bot.services.users import is_subscription_active


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

        # Check onboarding first — if not completed, skip subscription CTA entirely
        # (onboarding gate in handlers will show the correct CTA)
        try:
            onboarding_exists = await session.scalar(
                select(OnboardingAnswerModel.id).where(OnboardingAnswerModel.user_id == user.id)
            )
            if not onboarding_exists:
                return False  # Let handler's onboarding gate handle this
        except Exception:
            pass

        # Do not send CTA for any callbacks at all — CTA is only for messages
        try:
            if getattr(event, "data", None) is not None:
                return False
        except Exception:
            pass

        db_user = await session.get(UserModel, user.id)
        has_foodai = (getattr(db_user, "foodai_enabled_at", None) is not None) if db_user is not None else False
        # Admin bypass: admins can always use FoodAI
        if db_user is not None and bool(getattr(db_user, "is_admin", False)):
            if not has_foodai:
                try:
                    await session.execute(
                        update(UserModel).where(UserModel.id == user.id).values(foodai_enabled_at=func.now())
                    )
                    try:
                        await session.commit()
                    except Exception:
                        pass
                except Exception:
                    pass
            return True

        active = await is_subscription_active(session, user.id, include_grace=True)
        if active:
            # Lazily enable flag if missing
            if (db_user is not None) and (not has_foodai):
                try:
                    await session.execute(
                        update(UserModel).where(UserModel.id == user.id).values(foodai_enabled_at=func.now())
                    )
                    try:
                        await session.commit()
                    except Exception:
                        pass
                except Exception:
                    pass
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

            text = (
                "😴 Подписка не активна\n"
                "С каждым днём ты можешь быть ближе к цели, но без анализа это движение вслепую. \n"
                "calorissimo_ai_bot поможет тебе достичь нужного результата"
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="💎 Выбрать тариф", callback_data="sale:choose")]]
            )
            # Prefer replying in chat context (message)
            if msg_obj and hasattr(msg_obj, "answer"):
                try:
                    await msg_obj.answer(text, reply_markup=kb, disable_web_page_preview=True)
                    return False
                except Exception:
                    pass
            # Final fallback (if event is a Message implementing answer)
            if hasattr(event, "answer"):
                try:
                    await event.answer(text, reply_markup=kb, disable_web_page_preview=True)
                except Exception:
                    pass
        except Exception:
            pass
        return False
