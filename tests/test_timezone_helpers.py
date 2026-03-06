import importlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest


@pytest.mark.asyncio
async def test_get_user_tzinfo_fallback_default(monkeypatch) -> None:
    users_mod = importlib.import_module("bot.services.users")
    # get_timezone returns empty -> fallback to DEFAULT_TZ
    async def _fake_get_timezone(session, uid) -> str:
        return ""
    monkeypatch.setattr(users_mod, "get_timezone", _fake_get_timezone, raising=False)
    # Patch settings.DEFAULT_TZ
    cfg_mod = importlib.import_module("bot.core.config")
    getattr(cfg_mod.settings, "DEFAULT_TZ", None)
    monkeypatch.setattr(cfg_mod.settings, "DEFAULT_TZ", "Europe/Moscow", raising=False)
    tzinfo = await users_mod.get_user_tzinfo(None, 123)
    assert isinstance(tzinfo, ZoneInfo)
    assert tzinfo.key in {"Europe/Moscow"}
    # Restore (monkeypatch auto-restores after test)


@pytest.mark.asyncio
async def test_get_user_tzinfo_invalid_fallback_utc(monkeypatch) -> None:
    users_mod = importlib.import_module("bot.services.users")
    async def _fake_get_timezone(session, uid) -> str:
        return "Not/AZone"
    monkeypatch.setattr(users_mod, "get_timezone", _fake_get_timezone, raising=False)
    # Ensure default tz doesn't interfere
    cfg_mod = importlib.import_module("bot.core.config")
    monkeypatch.setattr(cfg_mod.settings, "DEFAULT_TZ", "UTC", raising=False)
    tzinfo = await users_mod.get_user_tzinfo(None, 1)
    assert tzinfo is timezone.utc


@pytest.mark.asyncio
async def test_today_local_utc_dates_kiritimati(monkeypatch) -> None:
    """UTC+14 should span two UTC dates for local 'today'."""
    users_mod = importlib.import_module("bot.services.users")
    # Force tzinfo to Pacific/Kiritimati (UTC+14)
    async def _fake_get_tzinfo(session, uid):
        return ZoneInfo("Pacific/Kiritimati")
    monkeypatch.setattr(users_mod, "get_user_tzinfo", _fake_get_tzinfo, raising=False)

    # Freeze datetime.now in users module to a fixed local time
    fixed_local = datetime(2025, 1, 1, 0, 30, tzinfo=ZoneInfo("Pacific/Kiritimati"))

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_local if tz is None else fixed_local.astimezone(tz)

    monkeypatch.setattr(users_mod, "datetime", FixedDateTime, raising=False)

    dates = await users_mod.today_local_utc_dates(None, 123)
    # Local day 2025-01-01 in UTC+14 spans 2024-12-31 and 2025-01-01 in UTC
    assert {d.isoformat() for d in dates} == {"2024-12-31", "2025-01-01"}


@pytest.mark.asyncio
async def test_today_local_utc_dates_los_angeles(monkeypatch) -> None:
    """UTC-8 (PST) local day should span the current and next UTC dates."""
    users_mod = importlib.import_module("bot.services.users")
    async def _fake_get_tzinfo(session, uid):
        return ZoneInfo("America/Los_Angeles")
    monkeypatch.setattr(users_mod, "get_user_tzinfo", _fake_get_tzinfo, raising=False)

    fixed_local = datetime(2025, 1, 1, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_local if tz is None else fixed_local.astimezone(tz)

    monkeypatch.setattr(users_mod, "datetime", FixedDateTime, raising=False)

    dates = await users_mod.today_local_utc_dates(None, 777)
    assert {d.isoformat() for d in dates} == {"2025-01-01", "2025-01-02"}


@pytest.mark.asyncio
async def test_today_local_utc_dates_utc_single_date(monkeypatch) -> None:
    users_mod = importlib.import_module("bot.services.users")
    async def _fake_get_tzinfo(session, uid):
        return timezone.utc
    monkeypatch.setattr(users_mod, "get_user_tzinfo", _fake_get_tzinfo, raising=False)

    fixed_utc = datetime(2025, 1, 2, 10, 0, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_utc if tz is None else fixed_utc.astimezone(tz)

    monkeypatch.setattr(users_mod, "datetime", FixedDateTime, raising=False)

    dates = await users_mod.today_local_utc_dates(None, 42)
    # For UTC, both boundaries map to the same UTC date
    assert {d.isoformat() for d in dates} == {"2025-01-02"}
