# tests/test_foodai_precheck.py
from __future__ import annotations
import asyncio
from types import SimpleNamespace

from bot.services import foodai as foodai_mod


def test_photo_precheck_failure_returns_provider_error(monkeypatch) -> None:
    """If pre-check returns None twice (even after retry) -> provider_error, not not_food."""

    async def _run() -> None:
        # Force provider path and stable URL
        monkeypatch.setattr(foodai_mod, "_use_openai", lambda: True)

        async def fake_tg_file_url(fid: str) -> str:
            return "https://api.telegram.org/file/botTOKEN/photos/file_9.jpg"

        monkeypatch.setattr(foodai_mod, "_tg_file_url", fake_tg_file_url)

        # Pre-check returns None twice (simulate transient failure not recovered by retry)
        calls = SimpleNamespace(n=0)

        async def fake_foodness_photo(url: str) -> None:
            calls.n += 1

        monkeypatch.setattr(foodai_mod, "_foodness_photo", fake_foodness_photo)

        res = await foodai_mod.analyze_photo("FAKE_FILE_ID")
        assert isinstance(res, dict)
        assert res.get("error") == "provider_unavailable"

    asyncio.run(_run())


def test_photo_precheck_retry_none_then_false_returns_not_food(monkeypatch) -> None:
    """If pre-check returns None, then False on retry -> not_food true structure, not provider_error."""

    async def _run() -> None:
        monkeypatch.setattr(foodai_mod, "_use_openai", lambda: True)

        async def fake_tg_file_url(fid: str) -> str:
            return "https://api.telegram.org/file/botTOKEN/photos/file_9.jpg"

        monkeypatch.setattr(foodai_mod, "_tg_file_url", fake_tg_file_url)

        # First call returns None, second returns False
        seq = iter([None, False])

        async def fake_foodness_photo(url: str):
            try:
                return next(seq)
            except StopIteration:
                return False

        monkeypatch.setattr(foodai_mod, "_foodness_photo", fake_foodness_photo)

        res = await foodai_mod.analyze_photo("FAKE_FILE_ID")
        assert isinstance(res, dict)
        assert res.get("error") is None
        assert res.get("not_food") is True
        assert int(res.get("calories") or 0) == 0

    asyncio.run(_run())
