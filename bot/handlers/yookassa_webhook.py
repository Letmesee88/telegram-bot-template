from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aiohttp.web import Response, View
from loguru import logger
from yookassa import Configuration, Payment

from bot.core.config import settings
from bot.core.loader import bot
from bot.database.database import sessionmaker
from bot.database.models import PaymentModel, SubscriptionModel, UserModel
from sqlalchemy import select, update, func
from bot.services.users import get_user_tzinfo


def _ensure_yk_configured() -> bool:
    if not settings.YOOKASSA_SHOP_ID or not settings.YOOKASSA_SECRET_KEY:
        return False
    Configuration.configure(settings.YOOKASSA_SHOP_ID, settings.YOOKASSA_SECRET_KEY)
    return True


def _expected_amount(plan: str) -> Decimal:
    if plan == "trial":
        return Decimal(str(settings.PRICE_TRIAL_RUB))
    if plan == "month":
        return Decimal(str(settings.PRICE_MONTH_RUB))
    if plan == "year":
        return Decimal(str(settings.PRICE_YEAR_RUB))
    return Decimal("0")


def _add_duration(plan: str, base: datetime) -> datetime:
    if plan == "trial":
        return base + timedelta(days=3)
    if plan == "month":
        return base + timedelta(days=30)
    if plan == "year":
        return base + timedelta(days=365)
    return base


class YooKassaWebhookView(View):
    async def post(self) -> Response:
        try:
            payload = await self.request.json()
        except Exception as e:
            logger.warning(f"yookassa webhook parse error: {e}")
            return Response(text="OK")

        event = payload.get("event")
        obj = payload.get("object") or {}
        pid = obj.get("id")
        logger.info(f"yk.webhook.received | event={event} | payment_id={pid}")
        # Handle cancelation explicitly: notify user and mark payment canceled if known
        if event == "payment.canceled":
            payment_id = pid
            # Prefer user_id from webhook payload metadata to avoid extra API calls
            user_id = None
            try:
                md = obj.get("metadata") or {}
                if isinstance(md, dict) and md.get("user_id") is not None:
                    user_id = int(md.get("user_id"))
            except Exception:
                user_id = None

            if user_id is None and payment_id:
                # Fallback: fetch payment from YooKassa API to extract metadata.user_id
                yk_payment = None
                try:
                    if _ensure_yk_configured():
                        yk_payment = await asyncio.to_thread(Payment.find_one, payment_id)
                    else:
                        logger.warning("yookassa not configured; skip find_one for payment.canceled")
                except Exception as e:
                    logger.warning(f"yookassa find_one failed for canceled: {e}")
                    yk_payment = None
                if yk_payment is not None:
                    try:
                        md = getattr(yk_payment, "metadata", {}) or {}
                        uid = md.get("user_id")
                        user_id = int(uid) if uid is not None else None
                    except Exception:
                        user_id = None
            logger.info(f"yk.webhook.canceled | payment_id={payment_id} | user_id={user_id}")
            # Best-effort DB update
            if payment_id:
                async with sessionmaker() as session:
                    try:
                        res = await session.execute(select(PaymentModel).where(PaymentModel.yk_payment_id == payment_id))
                        pm = res.scalar_one_or_none()
                        if pm and getattr(pm, "status", None) != "canceled":
                            await session.execute(
                                update(PaymentModel)
                                .where(PaymentModel.id == pm.id)
                                .values(status="canceled")
                            )
                            await session.commit()
                            logger.info(f"yk.webhook.canceled.db_updated | payment_db_id={getattr(pm, 'id', None)}")
                    except Exception:
                        pass
            if user_id:
                try:
                    await bot.send_message(user_id, "❌ Что-то пошло не так. Попробуйте еще раз.")
                    logger.info(f"yk.webhook.canceled.notified | user_id={user_id}")
                except Exception:
                    pass
            return Response(text="OK")

        if event != "payment.succeeded":
            logger.info(f"yk.webhook.ignored | event={event} | payment_id={pid}")
            return Response(text="OK")

        if not _ensure_yk_configured():
            logger.warning("yookassa not configured; skip")
            return Response(text="OK")

        obj = payload.get("object") or {}
        payment_id = obj.get("id")
        if not payment_id:
            return Response(text="OK")

        try:
            yk_payment = await asyncio.to_thread(Payment.find_one, payment_id)
        except Exception as e:
            logger.warning(f"yookassa fetch error: {e}")
            return Response(text="OK")

        try:
            status = getattr(yk_payment, "status", None)
            amount_value = Decimal(str(getattr(yk_payment.amount, "value", "0")))
            currency = getattr(yk_payment.amount, "currency", "")
            metadata = getattr(yk_payment, "metadata", {}) or {}
            payment_method = getattr(yk_payment, "payment_method", None)
            payment_method_id = getattr(payment_method, "id", None) if payment_method else None
            plan = str(metadata.get("plan", "")).lower()
            user_id = int(metadata.get("user_id")) if metadata.get("user_id") else None
        except Exception as e:
            logger.warning(f"yookassa parse error: {e}")
            return Response(text="OK")

        if status != "succeeded" or currency != "RUB" or not user_id or plan not in {"trial", "month", "year"}:
            return Response(text="OK")

        expected = _expected_amount(plan)
        if expected <= 0 or amount_value != expected:
            logger.warning(f"yk.webhook.succeeded.amount_mismatch | got={amount_value} expected={expected} plan={plan} payment_id={payment_id}")
            return Response(text="OK")

        async with sessionmaker() as session:
            res = await session.execute(
                select(PaymentModel).where(PaymentModel.yk_payment_id == payment_id)
            )
            current_payment = res.scalar_one_or_none()
            p = None
            existing_db_id = None
            if current_payment is not None:
                existing_db_id = current_payment.id
                if getattr(current_payment, "status", None) == "succeeded":
                    return Response(text="OK")
                await session.execute(
                    update(PaymentModel)
                    .where(PaymentModel.id == existing_db_id)
                    .values(
                        amount_value=amount_value,
                        currency=currency,
                        status="succeeded",
                        description=getattr(yk_payment, "description", None),
                        meta=dict(metadata),
                        payment_method_id=payment_method_id,
                        captured_at_utc=datetime.now(timezone.utc),
                    )
                )
            else:
                p = PaymentModel(
                    user_id=user_id,
                    subscription_id=None,
                    yk_payment_id=payment_id,
                    idempotence_key=None,
                    payment_method_id=payment_method_id,
                    amount_value=amount_value,
                    currency=currency,
                    status="succeeded",
                    description=getattr(yk_payment, "description", None),
                    meta=dict(metadata),
                    captured_at_utc=datetime.now(timezone.utc),
                )
                session.add(p)

            sub = await session.execute(
                select(SubscriptionModel).where(SubscriptionModel.user_id == user_id)
            )
            current = sub.scalar_one_or_none()
            now = datetime.now(timezone.utc)
            if current is None:
                new_exp = _add_duration(plan, now)
                s = SubscriptionModel(
                    user_id=user_id,
                    status="active",
                    plan=plan,
                    payment_method_id=payment_method_id,
                    started_at_utc=now,
                    expires_at_utc=new_exp,
                )
                session.add(s)
                await session.flush()
                # Link payment to created subscription
                if p is not None:
                    p.subscription_id = s.id
                elif existing_db_id is not None:
                    await session.execute(
                        update(PaymentModel).where(PaymentModel.id == existing_db_id).values(subscription_id=s.id)
                    )
                exp_dt = new_exp
            else:
                # Stack duration only when the plan remains the same and the current subscription
                # is still active in the future. If user changes plan (e.g., year -> month),
                # start counting from now to avoid inflating expiry by stacking onto a far-future date.
                if (
                    current.expires_at_utc
                    and current.expires_at_utc > now
                    and (current.plan == plan)
                ):
                    base = current.expires_at_utc
                else:
                    base = now
                new_exp = _add_duration(plan, base)
                await session.execute(
                    update(SubscriptionModel)
                    .where(SubscriptionModel.id == current.id)
                    .values(
                        status="active",
                        plan=plan,
                        payment_method_id=payment_method_id or current.payment_method_id,
                        expires_at_utc=new_exp,
                    )
                )
                if p is not None:
                    p.subscription_id = current.id
                elif existing_db_id is not None:
                    await session.execute(
                        update(PaymentModel).where(PaymentModel.id == existing_db_id).values(subscription_id=current.id)
                    )
                exp_dt = new_exp

            await session.commit()

        try:
            # Format in user's timezone (fallback to DEFAULT_TZ/UTC handled by service)
            async with sessionmaker() as session:
                tzinfo = await get_user_tzinfo(session, user_id)
            _ = exp_dt.astimezone(tzinfo)  # compute to ensure tz is valid, but not shown per spec
            # Enable premium access and FoodAI for the user (idempotent)
            async with sessionmaker() as session:
                try:
                    await session.execute(
                        update(UserModel)
                        .where(UserModel.id == user_id)
                        .values(is_premium=True)
                    )
                    # Set foodai_enabled_at only if NULL
                    await session.execute(
                        update(UserModel)
                        .where(UserModel.id == user_id, UserModel.foodai_enabled_at.is_(None))
                        .values(foodai_enabled_at=func.now())
                    )
                    await session.commit()
                except Exception:
                    pass
            success_text = (
                "🎉 Подписка успешно оформлена!\n\n"
                "Супер! Теперь тебе доступны все возможности Calorissimo AI без ограничений\n\n"
                "Начинай путь к своей цели прямо сейчас! Что ты ел сегодня? Напиши текстом всё, что помнишь — мы сразу начнём считать твои калории. Есть фотографии блюд? Отправляй их тоже!"
            )
            await bot.send_message(user_id, success_text)
            logger.info(f"yk.webhook.succeeded.notified | user_id={user_id}")
        except Exception as e:
            logger.warning(f"notify user failed: {e}")

        return Response(text="OK")
