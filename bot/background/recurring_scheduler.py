from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from loguru import logger
from sqlalchemy import select, update

from bot.core.config import settings
from bot.core.loader import redis_client
from bot.database.database import sessionmaker
from bot.database.models import SubscriptionModel, UserModel
from bot.services.users import get_user_tzinfo
from bot.services import yookassa as yk

# Redis keys
ZSET_DUE = "rebill:due"  # zset of subscription_id -> next_attempt_epoch
LOCK_FMT = "rebill:lock:{sub_id}:{period}"
SUBMITTED_FMT = "rebill:submitted:{sub_id}:{period}"
ATTEMPTS_FMT = "rebill:attempts:{sub_id}:{period}"

DEFAULT_RETRY_DAYS = [0, 1, 3]
MAX_ATTEMPTS = len(DEFAULT_RETRY_DAYS)


@dataclass
class _RebillTask:
    subscription_id: int
    user_id: int
    plan: str
    payment_method_id: Optional[str]
    period_key: str  # YYYY-MM-DD of the expiring period


def _period_key(dt: datetime) -> str:
    try:
        return dt.date().isoformat()
    except Exception:
        # Fallback to UTC date now
        return datetime.now(timezone.utc).date().isoformat()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_local(tz) -> datetime:
    return datetime.now(tz)


async def _schedule_retry(sub_id: int, period_key: str, attempts_done: int) -> None:
    try:
        delays = getattr(settings, "REBILL_RETRY_DAYS", None) or DEFAULT_RETRY_DAYS
        # Safety: ensure list of ints
        delays = [int(x) for x in delays][:MAX_ATTEMPTS]
        if attempts_done >= len(delays):
            return
        delay_days = delays[attempts_done]
        next_ts = int((_now_utc() + timedelta(days=delay_days)).timestamp())
        await redis_client.zadd(ZSET_DUE, {f"{sub_id}:{period_key}": next_ts})
    except Exception:
        pass


async def _try_rebill(bot: Bot, task: _RebillTask) -> None:
    lock_key = LOCK_FMT.format(sub_id=task.subscription_id, period=task.period_key)
    got = await redis_client.set(lock_key, "1", nx=True, ex=120)
    if not got:
        return
    try:
        submitted_key = SUBMITTED_FMT.format(sub_id=task.subscription_id, period=task.period_key)
        if await redis_client.exists(submitted_key):
            return
        # Validate prerequisites
        if not task.payment_method_id:
            await _mark_past_due_and_notify(bot, task.subscription_id, task.user_id)
            return
        # Create rebill payment (no confirmation)
        try:
            result = await yk.create_recurring_payment(
                user_id=task.user_id,
                subscription_id=task.subscription_id,
                plan=task.plan,
                payment_method_id=task.payment_method_id,
                period_key=task.period_key,
            )
            # Mark submitted for this period to avoid duplicates
            await redis_client.set(submitted_key, result.idempotence_key or "1", ex=7 * 24 * 3600)
            logger.info(
                f"rebill.submitted | sub={task.subscription_id} user={task.user_id} plan={task.plan} period={task.period_key} payment_id={result.payment_id}"
            )
        except Exception as e:
            # Immediate failure — schedule retry and close access
            logger.warning(
                f"rebill.create_failed | sub={task.subscription_id} user={task.user_id} plan={task.plan} period={task.period_key} err={e}"
            )
            await _mark_past_due_and_notify(bot, task.subscription_id, task.user_id)
            # Increase attempts and schedule next
            attempts_key = ATTEMPTS_FMT.format(sub_id=task.subscription_id, period=task.period_key)
            attempts = int((await redis_client.incr(attempts_key)) or 1)
            await redis_client.expire(attempts_key, 15 * 24 * 3600)
            await _schedule_retry(task.subscription_id, task.period_key, attempts_done=attempts - 1)
    finally:
        try:
            await redis_client.delete(lock_key)
        except Exception:
            pass


async def _mark_past_due_and_notify(bot: Bot, sub_id: int, user_id: int) -> None:
    # Close access immediately
    async with sessionmaker() as session:
        try:
            await session.execute(
                update(SubscriptionModel).where(SubscriptionModel.id == sub_id).values(status="past_due")
            )
            await session.execute(
                update(UserModel).where(UserModel.id == user_id).values(is_premium=False)
            )
            await session.commit()
        except Exception:
            pass
    # Notify user (neutral)
    try:
        plan = None
        try:
            async with sessionmaker() as s2:
                res = await s2.execute(select(SubscriptionModel).where(SubscriptionModel.id == sub_id))
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
            user_id,
            "❌ Не удалось продлить подписку. Проверьте карту/средства/банк и попробуйте оплатить вручную в разделе \u00abПодписка\u00bb.",
            reply_markup=kb,
        )
    except Exception:
        pass


class RecurringScheduler:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self._sem = asyncio.Semaphore(int(getattr(settings, "REBILL_CONCURRENCY", 20) or 20))

    async def _scan_and_submit_initial(self) -> None:
        """Scan DB for subscriptions due for first attempt (expires_at <= now)."""
        now = _now_utc()
        async with sessionmaker() as session:
            res = await session.execute(
                select(SubscriptionModel).where(
                    SubscriptionModel.status == "active",
                    SubscriptionModel.auto_renew.is_(True),
                    SubscriptionModel.expires_at_utc.is_not(None),
                    SubscriptionModel.expires_at_utc <= now,
                )
            )
            subs = res.scalars().all()
        if not subs:
            return
        tasks: list[asyncio.Task] = []
        for sub in subs:
            # Determine plan to bill
            plan = getattr(sub, "next_plan", None) or ("year" if sub.plan == "trial" else sub.plan)
            period = _period_key(getattr(sub, "expires_at_utc", now))
            # Gate by 10:00 local time on expiry date
            try:
                async with sessionmaker() as s2:
                    tz = await get_user_tzinfo(s2, sub.user_id)
                now_local = _now_local(tz)
                exp_local_date = sub.expires_at_utc.astimezone(tz).date()
                hour = int(getattr(settings, "REBILL_HOUR", 10) or 10)
                if (now_local.date() < exp_local_date) or (
                    now_local.date() == exp_local_date and now_local.hour < hour
                ):
                    continue
            except Exception:
                pass
            t = _RebillTask(
                subscription_id=sub.id,
                user_id=sub.user_id,
                plan=plan,
                payment_method_id=sub.payment_method_id,
                period_key=period,
            )
            tasks.append(asyncio.create_task(self._bounded(_try_rebill, t)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_due_retries(self) -> None:
        now = int(time.time())
        due = await redis_client.zrangebyscore(ZSET_DUE, min="-inf", max=now, start=0, num=200, withscores=False)
        if not due:
            return
        if isinstance(due, (bytes, str)):
            due = [due]
        # Remove first to avoid dupes during processing
        pipe = redis_client.pipeline(transaction=False)
        for key in due:
            pipe.zrem(ZSET_DUE, key)
        try:
            await pipe.execute()
        except Exception:
            pass
        tasks: list[asyncio.Task] = []
        for entry in due:
            try:
                if isinstance(entry, bytes):
                    entry = entry.decode()
                sub_id_str, period = str(entry).split(":", 1)
                sub_id = int(sub_id_str)
            except Exception:
                continue
            # Load current sub/user
            async with sessionmaker() as session:
                res = await session.execute(select(SubscriptionModel).where(SubscriptionModel.id == sub_id))
                sub = res.scalar_one_or_none()
            if sub is None:
                continue
            # If subscription already extended (success happened), skip
            if getattr(sub, "expires_at_utc", None) and sub.expires_at_utc > _now_utc():
                # Success; clear attempts counter
                try:
                    await redis_client.delete(ATTEMPTS_FMT.format(sub_id=sub_id, period=period))
                except Exception:
                    pass
                continue
            plan = getattr(sub, "next_plan", None) or ("year" if sub.plan == "trial" else sub.plan)
            t = _RebillTask(
                subscription_id=sub.id,
                user_id=sub.user_id,
                plan=plan,
                payment_method_id=sub.payment_method_id,
                period_key=period,
            )
            tasks.append(asyncio.create_task(self._bounded(_try_rebill, t)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _bounded(self, func, task: _RebillTask) -> None:
        async with self._sem:
            await func(bot=self._bot, task=task)  # type: ignore[arg-type]

    async def run(self, bot: Bot) -> None:
        self._bot = bot
        interval = int(getattr(settings, "REBILL_SCAN_INTERVAL_SEC", 60) or 60)
        while not self._stopping.is_set():
            try:
                if not getattr(settings, "REBILL_ENABLED", True):
                    await asyncio.sleep(30)
                    continue
                await self._process_due_retries()
                await self._scan_and_submit_initial()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                try:
                    logger.warning(f"recurring_scheduler_loop_error | err={e}")
                except Exception:
                    pass
                await asyncio.sleep(2)

    async def start(self, bot: Bot) -> None:
        if self._task and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self.run(bot))

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass


recurring_scheduler = RecurringScheduler()
