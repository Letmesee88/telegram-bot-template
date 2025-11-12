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
from bot.database.models import PaymentModel, SubscriptionModel
from sqlalchemy import select, update


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
        if event != "payment.succeeded":
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
            logger.warning(f"amount mismatch: got {amount_value} expected {expected} plan={plan}")
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
                SubscriptionModel.__table__.select().where(SubscriptionModel.user_id == user_id)
            )
            row = sub.first()
            now = datetime.now(timezone.utc)
            if row is None:
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
                    from sqlalchemy import update
                    await session.execute(
                        update(PaymentModel).where(PaymentModel.id == existing_db_id).values(subscription_id=s.id)
                    )
                exp_dt = new_exp
            else:
                current = row[0]
                base = current.expires_at_utc if current.expires_at_utc and current.expires_at_utc > now else now
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
            until = exp_dt.astimezone(timezone.utc).strftime("%d.%m.%Y")
            await bot.send_message(user_id, f"Оплата получена ✅\nПодписка активна до {until}")
        except Exception as e:
            logger.warning(f"notify user failed: {e}")

        return Response(text="OK")
