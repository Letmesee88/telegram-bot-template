from __future__ import annotations
import asyncio
import contextlib
import random
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import select

from bot.analytics.types import BaseEvent, EventProperties, Plan
from bot.core.config import settings
from bot.core.loader import redis_client
from bot.database.database import sessionmaker
from bot.database.models import DailyIntakeModel, OnboardingAnswerModel, SubscriptionModel
from bot.metrics import daily_report_queue_lag_seconds
from bot.services.analytics import analytics
from bot.services.reports import assemble_and_send_report
from bot.services.users import get_user_tzinfo

if TYPE_CHECKING:
    from aiogram import Bot

ZSET_KEY = "reports:schedule"
LOCK_FMT = "reports:lock:{}"

ONB_ZSET_STARTED = "onboarding:started"
ONB_ABANDONED_SENT_FMT = "onboarding:abandoned_sent:{user_id}:{start_ts}"


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
        elif getattr(settings, "DAILY_REPORTS_REQUIRE_PREMIUM", False):
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
    for uid, sc in zip(uids, scores, strict=False):
        if sc is None:
            nxt = await _next_run_epoch(uid)
            to_add.append((uid, nxt))
    if to_add:
        pipe = redis_client.pipeline(transaction=False)
        for uid, score in to_add:
            pipe.zadd(ZSET_KEY, {uid: score}, nx=True)
        await pipe.execute()


async def _scan_onboarding_abandoned() -> None:
    if not analytics.logger:
        return
    now = int(time.time())
    cutoff = now - 24 * 3600
    try:
        due = await redis_client.zrangebyscore(ONB_ZSET_STARTED, min="-inf", max=cutoff, start=0, num=200, withscores=True)
    except Exception:
        return
    if not due:
        return
    tasks: list[asyncio.Task] = []
    for mem, score in due:
        try:
            user_id = int(mem)
            start_ts = int(score)
        except Exception:
            continue
        try:
            sent_key = ONB_ABANDONED_SENT_FMT.format(user_id=user_id, start_ts=start_ts)
            if await redis_client.exists(sent_key):
                await redis_client.zrem(ONB_ZSET_STARTED, user_id)
                continue
            last_step = await redis_client.get(f"onboarding:last_step:{user_id}")
            last_idx = await redis_client.get(f"onboarding:last_step_index:{user_id}")
            try:
                if isinstance(last_step, bytes):
                    last_step = last_step.decode()
            except Exception:
                pass
            try:
                if isinstance(last_idx, bytes):
                    last_idx = last_idx.decode()
            except Exception:
                pass
            age_hours = max(0, int((now - start_ts) // 3600))
            evt = BaseEvent(
                user_id=user_id,
                event_type="onboarding_abandoned",
                event_properties=EventProperties(
                    chat_id=None,
                    chat_type=None,
                    text=None,
                    command=None,
                    last_step_name=(str(last_step) if last_step is not None else None),
                    last_step_index=(int(last_idx) if last_idx is not None and str(last_idx).isdigit() else None),
                    age_hours=age_hours,
                ),
                language=getattr(settings, "DEFAULT_LOCALE", None),
                plan=Plan(branch="Onboarding", source="onboarding", version="v1"),
            )
            analytics.fire_event(evt)
            await redis_client.set(sent_key, "1", ex=7 * 24 * 3600)
            await redis_client.zrem(ONB_ZSET_STARTED, user_id)
        except Exception:
            continue
    if tasks:
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)


class ReportScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._sem = asyncio.Semaphore(int(getattr(settings, "DAILY_REPORTS_LLM_CONCURRENCY", 50) or 50))
        self._last_onb_scan_ts = 0

    async def _worker(self, bot: Bot, user_id: int, scheduled_epoch: int) -> None:
        lock_key = LOCK_FMT.format(user_id)
        got = await redis_client.set(lock_key, "1", nx=True, ex=60)
        if not got:
            return
        try:
            now = int(time.time())
            lag = max(0, now - int(scheduled_epoch or now))
            with contextlib.suppress(Exception):
                daily_report_queue_lag_seconds.observe(float(lag))
            async with self._sem:
                should_reschedule = await assemble_and_send_report(bot, user_id, scheduled_epoch=scheduled_epoch)
            if should_reschedule:
                nxt = await _next_run_epoch(user_id)
                await redis_client.zadd(ZSET_KEY, {user_id: nxt})
        finally:
            with contextlib.suppress(Exception):
                await redis_client.delete(lock_key)

    async def run(self, bot: Bot) -> None:
        await _seed_audience()
        while not self._stopping.is_set():
            try:
                try:
                    now = int(time.time())
                    if now - int(self._last_onb_scan_ts or 0) >= 300:
                        self._last_onb_scan_ts = now
                        await _scan_onboarding_abandoned()
                except Exception:
                    pass
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
                with contextlib.suppress(Exception):
                    logger.warning("report_scheduler_loop_error | err={}", e)
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
            with contextlib.suppress(Exception):
                await self._task


scheduler = ReportScheduler()
