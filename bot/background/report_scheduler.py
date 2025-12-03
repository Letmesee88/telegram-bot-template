from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timedelta, timezone, time as dtime
from typing import Optional

from aiogram import Bot
from loguru import logger
from sqlalchemy import select

from bot.core.config import settings
from bot.core.loader import redis_client
from bot.database.database import sessionmaker
from bot.database.models import OnboardingAnswerModel, UserModel, SubscriptionModel, DailyIntakeModel
from bot.services.users import get_user_tzinfo
from bot.services.reports import assemble_and_send_report
from bot.metrics import daily_report_queue_lag_seconds

ZSET_KEY = "reports:schedule"
LOCK_FMT = "reports:lock:{}"


def _jitter_minutes() -> int:
    try:
        j = int(getattr(settings, "DAILY_REPORTS_JITTER_MIN", 60) or 60)
    except Exception:
        j = 60
    return random.randint(0, max(0, j))


async def _next_run_epoch(user_id: int) -> int:
    async with sessionmaker() as session:
        tz = await get_user_tzinfo(session, user_id)
        now_local = datetime.now(tz)
        target = datetime.combine(now_local.date(), dtime(int(getattr(settings, "DAILY_REPORTS_HOUR", 8) or 8), 0), tz)
        if now_local >= target:
            target = target + timedelta(days=1)
        target = target + timedelta(minutes=_jitter_minutes())
        return int(target.astimezone(timezone.utc).timestamp())


async def _seed_audience() -> None:
    async with sessionmaker() as session:
        try:
            days_req = int(getattr(settings, "DAILY_REPORTS_REQUIRE_ACTIVITY_DAYS", 0) or 0)
        except Exception:
            days_req = 0
        if days_req > 0:
            # Build a conservative superset of UTC dates covering the last X local days ending yesterday
            utc_dates: set[date] = set()
            today_utc = datetime.now(timezone.utc).date()
            for i in range(1, days_req + 2):  # include yesterday .. X days back (+1 for TZ overlap)
                utc_dates.add(today_utc - timedelta(days=i))

            base = select(OnboardingAnswerModel.user_id).join(
                DailyIntakeModel, DailyIntakeModel.user_id == OnboardingAnswerModel.user_id
            ).where(
                DailyIntakeModel.date_utc.in_(utc_dates)
            )
            if getattr(settings, "DAILY_REPORTS_REQUIRE_PREMIUM", False):
                now = datetime.now(timezone.utc)
                base = (
                    base.join(SubscriptionModel, SubscriptionModel.user_id == OnboardingAnswerModel.user_id)
                    .where(
                        SubscriptionModel.status == "active",
                        SubscriptionModel.expires_at_utc.is_not(None),
                        SubscriptionModel.expires_at_utc > now,
                    )
                )
            res = await session.execute(base.distinct())
        else:
            if getattr(settings, "DAILY_REPORTS_REQUIRE_PREMIUM", False):
                now = datetime.now(timezone.utc)
                res = await session.execute(
                    select(OnboardingAnswerModel.user_id)
                    .join(SubscriptionModel, SubscriptionModel.user_id == OnboardingAnswerModel.user_id)
                    .where(
                        SubscriptionModel.status == "active",
                        SubscriptionModel.expires_at_utc.is_not(None),
                        SubscriptionModel.expires_at_utc > now,
                    )
                    .distinct()
                )
            else:
                res = await session.execute(select(OnboardingAnswerModel.user_id).distinct())
        uids = [int(x) for x in res.scalars().all()]
    if not uids:
        return
    pipe = redis_client.pipeline(transaction=False)
    for uid in uids:
        pipe.zscore(ZSET_KEY, uid)
    scores = await pipe.execute()
    to_add: list[tuple[int, int]] = []
    for uid, sc in zip(uids, scores):
        if sc is None:
            nxt = await _next_run_epoch(uid)
            to_add.append((uid, nxt))
    if to_add:
        pipe = redis_client.pipeline(transaction=False)
        for uid, score in to_add:
            pipe.zadd(ZSET_KEY, {uid: score}, nx=True)
        await pipe.execute()


class ReportScheduler:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self._sem = asyncio.Semaphore(int(getattr(settings, "DAILY_REPORTS_LLM_CONCURRENCY", 50) or 50))

    async def _worker(self, bot: Bot, user_id: int, scheduled_epoch: int) -> None:
        lock_key = LOCK_FMT.format(user_id)
        got = await redis_client.set(lock_key, "1", nx=True, ex=60)
        if not got:
            return
        try:
            now = int(time.time())
            lag = max(0, now - int(scheduled_epoch or now))
            try:
                daily_report_queue_lag_seconds.observe(float(lag))
            except Exception:
                pass
            async with self._sem:
                should_reschedule = await assemble_and_send_report(bot, user_id, scheduled_epoch=scheduled_epoch)
            if should_reschedule:
                nxt = await _next_run_epoch(user_id)
                await redis_client.zadd(ZSET_KEY, {user_id: nxt})
        finally:
            try:
                await redis_client.delete(lock_key)
            except Exception:
                pass

    async def run(self, bot: Bot) -> None:
        await _seed_audience()
        while not self._stopping.is_set():
            try:
                if not getattr(settings, "DAILY_REPORTS_ENABLED", True):
                    await asyncio.sleep(30)
                    continue
                now = int(time.time())
                batch = int(getattr(settings, "DAILY_REPORTS_BATCH_LIMIT", 200) or 200)
                due = await redis_client.zrangebyscore(ZSET_KEY, min="-inf", max=now, start=0, num=batch, withscores=True)
                if not due:
                    await asyncio.sleep(5)
                    continue
                tasks: list[asyncio.Task] = []
                for mem, score in due:
                    try:
                        uid = int(mem)
                    except Exception:
                        continue
                    tasks.append(asyncio.create_task(self._worker(bot, uid, int(score))))
                    # remove from zset early; _worker will re-add next run or lock prevents dupes
                    await redis_client.zrem(ZSET_KEY, uid)
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                break
            except Exception as e:
                try:
                    logger.warning("report_scheduler_loop_error | err={}", e)
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


scheduler = ReportScheduler()
