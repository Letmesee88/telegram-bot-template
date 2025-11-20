from __future__ import annotations

import pytest

from bot.handlers.onboarding import sale_trial, sale_buy_month, sale_buy_year


class _DummyMessage:
    def __init__(self) -> None:
        self.last_text: str | None = None

    async def edit_text(self, text: str, reply_markup=None, disable_web_page_preview: bool | None = None):
        self.last_text = text

    async def answer(self, text: str, reply_markup=None, disable_web_page_preview: bool | None = None):
        self.last_text = text


class _DummyFromUser:
    def __init__(self, uid: int) -> None:
        self.id = uid
        self.language_code = "ru"


class _DummyCall:
    def __init__(self, uid: int) -> None:
        self.from_user = _DummyFromUser(uid)
        self.message = _DummyMessage()
        self.data = ""

    async def answer(self):
        return None


@pytest.mark.asyncio
async def test_trial_screen_contains_disclaimer(test_db_env, ensure_user):
    uid = await ensure_user(10051)
    call = _DummyCall(uid)

    await sale_trial(call, state=None)  # type: ignore[arg-type]

    text = call.message.last_text or ""
    assert "Оплачивая, ты соглашаешься на сохранение способа оплаты для автопродления." in text
    assert "Автосписание можно отключить в разделе «Настройки → Подписка»." in text


@pytest.mark.asyncio
async def test_month_screen_contains_disclaimer(test_db_env, ensure_user):
    uid = await ensure_user(10052)
    call = _DummyCall(uid)

    await sale_buy_month(call, state=None)  # type: ignore[arg-type]

    text = call.message.last_text or ""
    assert "После оплаты подписка будет автоматически продлеваться." in text
    assert "Оплачивая, ты соглашаешься на сохранение способа оплаты для автопродления." in text
    assert "Автосписание можно отключить в разделе «Настройки → Подписка»." in text


@pytest.mark.asyncio
async def test_year_screen_contains_disclaimer(test_db_env, ensure_user):
    uid = await ensure_user(10053)
    call = _DummyCall(uid)

    await sale_buy_year(call, state=None)  # type: ignore[arg-type]

    text = call.message.last_text or ""
    assert "После оплаты подписка будет автоматически продлеваться." in text
    assert "Оплачивая, ты соглашаешься на сохранение способа оплаты для автопродления." in text
    assert "Автосписание можно отключить в разделе «Настройки → Подписка»." in text
