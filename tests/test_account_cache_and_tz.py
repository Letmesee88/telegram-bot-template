from __future__ import annotations

import pytest
from sqlalchemy import select

from bot.database.database import sessionmaker
from bot.database.models import OnboardingAnswerModel, WeightLogModel
from bot.services import users as users_service
from bot.services.account import get_account_summary_text
from bot.services.weight import save_weight


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.get_calls = 0
        self.set_calls = 0
        self.delete_calls = 0
        self.deleted_keys: list[str] = []

    async def get(self, key: str):
        self.get_calls += 1
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.set_calls += 1
        self.store[key] = value.encode("utf-8")

    async def delete(self, key: str) -> None:
        self.delete_calls += 1
        self.store.pop(key, None)
        self.deleted_keys.append(key)


@pytest.mark.asyncio
async def test_account_cache_hit_and_invalidation(monkeypatch, ensure_user) -> None:
    user_id = await ensure_user(user_id=22001)

    # Provide onboarding baseline
    async with sessionmaker() as session:
        oa = OnboardingAnswerModel(
            user_id=user_id,
            data={
                "weight_kg": 95.0,
                "start_weight_kg": 100.0,
                "goal_weight_kg": 80.0,
            },
            daily_plan={"calories": 0},
            goal="lose",
            calories=0,
        )
        session.add(oa)
        await session.commit()

    # Patch redis to fake
    fake = FakeRedis()
    import bot.cache.redis as cache_redis
    import bot.services.account as account_module
    import bot.services.weight as weight_module
    from bot.core import loader
    monkeypatch.setattr(loader, "redis_client", fake, raising=True)
    monkeypatch.setattr(weight_module, "redis_client", fake, raising=True)
    monkeypatch.setattr(cache_redis, "redis_client", fake, raising=True)
    monkeypatch.setattr(account_module, "redis_client", fake, raising=True)

    # First call -> computes and sets cache
    text1 = await get_account_summary_text(user_id)
    assert fake.set_calls == 1
    assert fake.get_calls == 1  # get tried first

    # Second call -> must come from cache (no new setex)
    text2 = await get_account_summary_text(user_id)
    assert text2 == text1
    assert fake.get_calls >= 2
    assert fake.set_calls == 1

    # Now save weight -> should delete cache key
    await save_weight(user_id, 90.0)
    assert fake.delete_calls == 1
    assert any(k.endswith(f"{user_id}") for k in fake.deleted_keys)

    # Next call -> recompute and set cache again
    text3 = await get_account_summary_text(user_id)
    assert fake.set_calls == 2
    assert "Текущий: 90.0 кг" in text3


@pytest.mark.asyncio
async def test_save_weight_respects_user_timezone_for_local_date(monkeypatch, ensure_user) -> None:
    user_id = await ensure_user(user_id=22002)

    # Set user's timezone to America/Los_Angeles
    async with sessionmaker() as session:
        # Patch redis clients to avoid real connection during clear_cache
        fake = FakeRedis()
        import bot.cache.redis as cache_redis
        import bot.services.weight as weight_module
        from bot.core import loader
        monkeypatch.setattr(loader, "redis_client", fake, raising=True)
        monkeypatch.setattr(weight_module, "redis_client", fake, raising=True)
        monkeypatch.setattr(cache_redis, "redis_client", fake, raising=True)
        await users_service.set_timezone(session, user_id, "America/Los_Angeles")

    # Save weight now
    value, local_date = await save_weight(user_id, 70.5)
    assert value == 70.5

    # Verify recorded_local_date equals the local_date returned by save_weight (robust against midnight boundary)
    async with sessionmaker() as session:
        wl = await session.scalar(
            select(WeightLogModel).where(
                (WeightLogModel.user_id == user_id) & (WeightLogModel.recorded_local_date == local_date)
            )
        )
    assert wl is not None
    assert wl.recorded_local_date == local_date
