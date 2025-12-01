from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aiohttp.web import Response, View
from loguru import logger
from yookassa import Configuration, Payment
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot.core.config import settings
from bot.core.loader import bot, redis_client
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
        # Handle cancelation explicitly with strict verification
        if event == "payment.canceled":
            payment_id = pid
            if not payment_id:
                return Response(text="OK")

            # Always fetch canonical payment from YooKassa to confirm authenticity
            if not _ensure_yk_configured():
                logger.warning("yookassa not configured; skip canceled")
                return Response(text="OK")
            try:
                yk_payment = await asyncio.to_thread(Payment.find_one, payment_id)
            except Exception as e:
                logger.warning(f"yookassa find_one failed for canceled: {e}")
                return Response(text="OK")

            # Extract trusted metadata from YooKassa API response
            try:
                md = getattr(yk_payment, "metadata", {}) or {}
            except Exception:
                md = {}
            user_id = None
            is_rebill = False
            sub_id_from_meta = None
            period_key = None
            try:
                if md.get("user_id") is not None:
                    user_id = int(md.get("user_id"))
            except Exception:
                user_id = None
            try:
                is_rebill = bool(md.get("rebill"))
            except Exception:
                is_rebill = False
            try:
                sub_id_from_meta = int(md.get("subscription_id")) if md.get("subscription_id") is not None else None
            except Exception:
                sub_id_from_meta = None
            try:
                period_key = md.get("period_key")
            except Exception:
                period_key = None

            # Cancellation diagnostic info from webhook payload (best-effort)
            cd = obj.get("cancellation_details") or {}
            try:
                logger.info(
                    f"yk.webhook.canceled | payment_id={payment_id} | user_id={user_id}"
                )
                party = cd.get("party") if isinstance(cd, dict) else None
                reason = cd.get("reason") if isinstance(cd, dict) else None
                if party or reason:
                    logger.info(
                        f"yk.webhook.canceled.details | party={party} | reason={reason}"
                    )
            except Exception:
                pass

            # Cross-check with our DB payment record; ignore unknown payment IDs
            async with sessionmaker() as session:
                res = await session.execute(select(PaymentModel).where(PaymentModel.yk_payment_id == payment_id))
                pm = res.scalar_one_or_none()
            if pm is None:
                logger.warning("yk.webhook.canceled.unknown_payment | payment_id={}", payment_id)
                return Response(text="OK")

            # Update payment status to canceled and merge cancellation details
            try:
                async with sessionmaker() as session:
                    try:
                        existing_meta = dict(getattr(pm, "meta", {}) or {})
                        if isinstance(cd, dict) and cd:
                            existing_meta["cancellation_details"] = cd
                    except Exception:
                        existing_meta = getattr(pm, "meta", None)
                    await session.execute(
                        update(PaymentModel)
                        .where(PaymentModel.id == pm.id)
                        .values(status="canceled", meta=existing_meta)
                    )
                    await session.commit()
            except Exception:
                pass

            # Rebill cancellation flow (guarded): act only when subscription matches our payment record
            if is_rebill and sub_id_from_meta and period_key and (getattr(pm, "subscription_id", None) == sub_id_from_meta):
                try:
                    async with sessionmaker() as session:
                        try:
                            # Close access now
                            await session.execute(
                                update(SubscriptionModel).where(SubscriptionModel.id == sub_id_from_meta).values(status="past_due")
                            )
                            # Prefer user_id from DB payment if missing in metadata
                            uid = user_id or getattr(pm, "user_id", None)
                            if uid:
                                await session.execute(
                                    update(UserModel)
                                    .where(UserModel.id == uid)
                                    .values(is_premium=False, foodai_enabled_at=None)
                                )
                            await session.commit()
                        except Exception:
                            pass
                    # Check permanent cancellation reasons — if permanent, disable auto_renew and clear payment_method_id
                    try:
                        reason = None
                        if isinstance(cd, dict):
                            reason = cd.get("reason")
                    except Exception:
                        reason = None
                    permanent_reasons = {"permission_revoked", "payment_method_restricted"}
                    is_permanent = bool(reason in permanent_reasons)
                    if is_permanent:
                        try:
                            async with sessionmaker() as session:
                                await session.execute(
                                    update(SubscriptionModel)
                                    .where(SubscriptionModel.id == sub_id_from_meta)
                                    .values(auto_renew=False, payment_method_id=None)
                                )
                                await session.commit()
                        except Exception:
                            pass
                    # Clear submitted flag to allow a new attempt (only if not permanent)
                    try:
                        if not is_permanent:
                            submitted_key = f"rebill:submitted:{sub_id_from_meta}:{period_key}"
                            await redis_client.delete(submitted_key)
                    except Exception:
                        pass
                    # Schedule next retry via Redis (skip if permanent)
                    try:
                        if is_permanent:
                            # Also clear attempts key if present
                            try:
                                attempts_key = f"rebill:attempts:{sub_id_from_meta}:{period_key}"
                                await redis_client.delete(attempts_key)
                            except Exception:
                                pass
                        else:
                            delays = getattr(settings, "REBILL_RETRY_DAYS", None) or [0, 1, 3]
                            delays = [int(x) for x in delays]
                    except Exception:
                        delays = [0, 1, 3]
                    # Count attempt and compute next (idempotent per sub+period)
                    if not is_permanent:
                        try:
                            processed_key = f"rebill:canceled:processed:{sub_id_from_meta}:{period_key}"
                            first = await redis_client.set(processed_key, "1", nx=True, ex=15 * 24 * 3600)
                        except Exception:
                            first = True
                        if not first:
                            return Response(text="OK")
                        attempts_key = f"rebill:attempts:{sub_id_from_meta}:{period_key}"
                        try:
                            attempts = int((await redis_client.incr(attempts_key)) or 1)
                        except Exception:
                            attempts = 1
                        try:
                            await redis_client.expire(attempts_key, 15 * 24 * 3600)
                        except Exception:
                            pass
                        if attempts <= len(delays):
                            try:
                                next_ts = int((datetime.now(timezone.utc) + timedelta(days=delays[attempts - 1])).timestamp())
                                zkey = "rebill:due"
                                member = f"{sub_id_from_meta}:{period_key}"
                                await redis_client.zadd(zkey, {member: next_ts})
                                logger.info(
                                    f"rebill.retry_scheduled | sub={sub_id_from_meta} period={period_key} attempt={attempts} at={next_ts}"
                                )
                            except Exception:
                                pass
                except Exception:
                    pass

            # Notify user (best-effort)
            uid = user_id or getattr(pm, "user_id", None)
            if uid:
                try:
                    if is_rebill:
                        plan = None
                        try:
                            if sub_id_from_meta:
                                async with sessionmaker() as s2:
                                    res = await s2.execute(select(SubscriptionModel).where(SubscriptionModel.id == sub_id_from_meta))
                                    sub = res.scalar_one_or_none()
                                    if sub is not None:
                                        plan = getattr(sub, "next_plan", None) or ("year" if sub.plan == "trial" else sub.plan)
                        except Exception:
                            plan = None
                        kb = None
                        try:
                            if plan == "month":
                                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Оплатить 750 руб", callback_data="sale:pay:month")]])
                            elif plan == "year":
                                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Оплатить 2500 руб", callback_data="sale:pay:year")]])
                            else:
                                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Оформить подписку", callback_data="sale:choose")]])
                        except Exception:
                            kb = None
                        await bot.send_message(
                            uid,
                            "❌ Не удалось продлить подписку. Проверьте карту/средства/банк и попробуйте оплатить вручную в разделе \u00abПодписка\u00bb.",
                            reply_markup=kb,
                        )
                    else:
                        await bot.send_message(uid, "❌ Что-то пошло не так. Попробуйте еще раз.")
                    logger.info(f"yk.webhook.canceled.notified | user_id={uid}")
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
            payment_method_saved = bool(getattr(payment_method, "saved", False)) if payment_method else False
            plan = str(metadata.get("plan", "")).lower()
            user_id = int(metadata.get("user_id")) if metadata.get("user_id") else None
            is_rebill = bool(metadata.get("rebill"))
            receipt_registration = getattr(yk_payment, "receipt_registration", None)
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
                # merge meta and add receipt_registration if present
                new_meta = dict(metadata)
                if receipt_registration:
                    try:
                        curm = getattr(current_payment, "meta", {}) or {}
                        curm = dict(curm)
                    except Exception:
                        curm = {}
                    curm.update(new_meta)
                    curm["receipt_registration"] = receipt_registration
                    new_meta = curm
                await session.execute(
                    update(PaymentModel)
                    .where(PaymentModel.id == existing_db_id)
                    .values(
                        amount_value=amount_value,
                        currency=currency,
                        status="succeeded",
                        description=getattr(yk_payment, "description", None),
                        meta=new_meta,
                        payment_method_id=payment_method_id,
                        captured_at_utc=datetime.now(timezone.utc),
                    )
                )
            else:
                new_meta = dict(metadata)
                if receipt_registration:
                    new_meta["receipt_registration"] = receipt_registration
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
                    meta=new_meta,
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
                    payment_method_id=payment_method_id if payment_method_saved else None,
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
                # Guard: don't downgrade active month/year to trial
                if (
                    plan == "trial"
                    and getattr(current, "status", None) == "active"
                    and getattr(current, "plan", None) in {"month", "year"}
                    and getattr(current, "expires_at_utc", None) is not None
                    and current.expires_at_utc > now
                ):
                    # Only link payment to existing subscription and optionally enable auto_renew
                    if p is not None:
                        p.subscription_id = current.id
                    elif existing_db_id is not None:
                        await session.execute(
                            update(PaymentModel).where(PaymentModel.id == existing_db_id).values(subscription_id=current.id)
                        )
                    # If a payment method was saved, ensure auto_renew is enabled (do not change plan/expiry)
                    if payment_method_saved:
                        try:
                            updates = {"auto_renew": True}
                            # Optionally fill missing payment_method_id
                            if not getattr(current, "payment_method_id", None) and payment_method_id:
                                updates["payment_method_id"] = payment_method_id
                            if updates:
                                await session.execute(
                                    update(SubscriptionModel)
                                    .where(SubscriptionModel.id == current.id)
                                    .values(**updates)
                                )
                        except Exception:
                            pass
                    await session.commit()
                    exp_dt = current.expires_at_utc
                    # Skip normal update path
                    return Response(text="OK")
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
                values = {
                    "status": "active",
                    "plan": plan,
                    "payment_method_id": (payment_method_id if payment_method_saved else current.payment_method_id),
                    "expires_at_utc": new_exp,
                }
                # Enable auto-renew on any non-rebill success when payment method is saved
                if not is_rebill and payment_method_saved:
                    values["auto_renew"] = True
                if is_rebill:
                    values["next_plan"] = None
                await session.execute(
                    update(SubscriptionModel)
                    .where(SubscriptionModel.id == current.id)
                    .values(**values)
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
            # Do not notify on recurring success
            if not is_rebill:
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
