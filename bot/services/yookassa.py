from __future__ import annotations
import asyncio
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from loguru import logger
from yookassa import Configuration, Payment

from bot.core.config import settings
from bot.database.database import sessionmaker
from bot.database.models import PaymentModel, UserModel
from sqlalchemy import select


@dataclass
class CreatedPayment:
    payment_id: str
    confirmation_url: str
    idempotence_key: str


def _configure() -> None:
    if not settings.YOOKASSA_SHOP_ID or not settings.YOOKASSA_SECRET_KEY:
        raise RuntimeError("YooKassa credentials are not configured")
    Configuration.configure(settings.YOOKASSA_SHOP_ID, settings.YOOKASSA_SECRET_KEY)


def _amount_for_plan(plan: str) -> Decimal:
    plan = plan.lower()
    if plan == "trial":
        return Decimal(str(settings.PRICE_TRIAL_RUB))
    if plan == "month":
        return Decimal(str(settings.PRICE_MONTH_RUB))
    if plan == "year":
        return Decimal(str(settings.PRICE_YEAR_RUB))
    raise ValueError(f"unknown plan: {plan}")


async def create_payment(user_id: int, plan: str, next_plan: str | None = None, return_url: str | None = None) -> CreatedPayment:
    _configure()

    plan = plan.lower()
    # Derive default next_plan if not explicitly provided
    eff_next_plan = next_plan
    if not eff_next_plan:
        if plan == "trial":
            eff_next_plan = "year"
        elif plan in {"month", "year"}:
            eff_next_plan = plan
    amount = _amount_for_plan(plan)
    idem = uuid4().hex
    amount_value = format(amount, ".2f")
    ret_url = return_url or getattr(settings, "WEBHOOK_BASE_URL", None)

    # Fetch customer email for fiscal receipt
    async with sessionmaker() as session:
        user_email = await session.scalar(
            select(UserModel.email).where(UserModel.id == user_id)  # type: ignore[name-defined]
        )
    if not user_email:
        raise RuntimeError("email_required")

    def _desc(p: str) -> str:
        if p == "trial":
            return "Подписка Calorissimo — пробный доступ (3 дня)"
        if p == "month":
            return "Подписка Calorissimo — 30 дней"
        if p == "year":
            return "Подписка Calorissimo — 365 дней"
        return f"Calorissimo {p}"

    payload: dict = {
        "amount": {"value": amount_value, "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect"},
        "description": f"Calorissimo {plan}",
        "metadata": {
            "user_id": user_id,
            "plan": plan,
        },
        # Force card and save PM for recurrent billing
        "payment_method_data": {"type": "bank_card"},
        "save_payment_method": True,
        "receipt": {
            "customer": {"email": user_email},
            "items": [
                {
                    "description": _desc(plan),
                    "quantity": 1.000,
                    "amount": {"value": amount_value, "currency": "RUB"},
                    "vat_code": 6,
                    "payment_mode": "full_prepayment",
                    "payment_subject": "service",
                }
            ],
        },
    }

    if eff_next_plan:
        payload["metadata"]["next_plan"] = eff_next_plan

    if ret_url:
        payload["confirmation"]["return_url"] = ret_url

    logger.info(f"YK create payment: user={user_id} plan={plan} next_plan={eff_next_plan} amount={amount}")
    try:
        yk_payment = await asyncio.to_thread(Payment.create, payload, idempotency_key=idem)
    except Exception as e:
        logger.error(f"YK create payment failed: {e}")
        raise

    payment_id: str = getattr(yk_payment, "id")
    confirmation = getattr(yk_payment, "confirmation", None)
    confirmation_url: str = getattr(confirmation, "confirmation_url", None) if confirmation else None
    if not confirmation_url:
        raise RuntimeError("No confirmation_url returned by YooKassa")
    logger.info(f"YK payment created: id={payment_id} idem={idem} url={confirmation_url}")

    async with sessionmaker() as session:
        p = PaymentModel(
            user_id=user_id,
            subscription_id=None,
            yk_payment_id=payment_id,
            idempotence_key=idem,
            payment_method_id=None,
            amount_value=amount,
            currency="RUB",
            status="pending",
            description=f"Calorissimo {plan}",
            meta={"user_id": user_id, "plan": plan, "next_plan": eff_next_plan} if eff_next_plan else {"user_id": user_id, "plan": plan},
        )
        session.add(p)
        await session.commit()
        logger.info(f"YK payment persisted: id={payment_id} status=pending user={user_id} plan={plan} amount={amount}")

    return CreatedPayment(payment_id=payment_id, confirmation_url=confirmation_url, idempotence_key=idem)


async def create_recurring_payment(
    *,
    user_id: int,
    subscription_id: int,
    plan: str,
    payment_method_id: str,
    period_key: str,
) -> CreatedPayment:
    """Create a recurring payment using saved payment_method_id (no user confirmation).

    Metadata will include `rebill=True`, `subscription_id`, and `period_key` to help webhook logic.
    Idempotency key is derived from subscription and period to guard against duplicate charges.
    """
    _configure()

    plan = plan.lower()
    amount = _amount_for_plan(plan)
    idem = f"rebill:{subscription_id}:{period_key}"
    amount_value = format(amount, ".2f")

    # Fetch customer email for fiscal receipt
    async with sessionmaker() as session:
        user_email = await session.scalar(
            select(UserModel.email).where(UserModel.id == user_id)  # type: ignore[name-defined]
        )
    if not user_email:
        raise RuntimeError("email_required")

    def _desc(p: str) -> str:
        if p == "trial":
            return "Подписка Calorissimo — пробный доступ (3 дня)"
        if p == "month":
            return "Подписка Calorissimo — 30 дней"
        if p == "year":
            return "Подписка Calorissimo — 365 дней"
        return f"Calorissimo {p}"

    payload: dict = {
        "amount": {"value": amount_value, "currency": "RUB"},
        "capture": True,
        "description": f"Calorissimo rebill {plan}",
        "payment_method_id": payment_method_id,
        "metadata": {
            "user_id": user_id,
            "plan": plan,
            "rebill": True,
            "subscription_id": subscription_id,
            "period_key": period_key,
        },
        "receipt": {
            "customer": {"email": user_email},
            "items": [
                {
                    "description": _desc(plan),
                    "quantity": 1.000,
                    "amount": {"value": amount_value, "currency": "RUB"},
                    "vat_code": 6,
                    "payment_mode": "full_prepayment",
                    "payment_subject": "service",
                }
            ],
        },
    }

    logger.info(
        f"YK create recurring: user={user_id} sub={subscription_id} plan={plan} amount={amount} pm_id={payment_method_id} period={period_key}"
    )
    try:
        yk_payment = await asyncio.to_thread(Payment.create, payload, idempotency_key=idem)
    except Exception as e:
        logger.error(f"YK create recurring failed: {e}")
        raise

    payment_id: str = getattr(yk_payment, "id")
    logger.info(f"YK recurring created: id={payment_id} idem={idem}")

    # Persist pending record for correlation with webhook
    async with sessionmaker() as session:
        p = PaymentModel(
            user_id=user_id,
            subscription_id=subscription_id,
            yk_payment_id=payment_id,
            idempotence_key=idem,
            payment_method_id=payment_method_id,
            amount_value=amount,
            currency="RUB",
            status="pending",
            description=f"Calorissimo rebill {plan}",
            meta={
                "user_id": user_id,
                "plan": plan,
                "rebill": True,
                "subscription_id": subscription_id,
                "period_key": period_key,
            },
        )
        session.add(p)
        await session.commit()
        logger.info(
            f"YK recurring persisted: id={payment_id} status=pending user={user_id} sub={subscription_id} plan={plan} amount={amount}"
        )

    # Reuse CreatedPayment container; confirmation_url is not used for recurring
    return CreatedPayment(payment_id=payment_id, confirmation_url="", idempotence_key=idem)
