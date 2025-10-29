import pytest

from bot.handlers.history import _truncate_wordwise, _compose_caption


def test_truncate_wordwise_basic():
    assert _truncate_wordwise("aaaa bbbb cccc", 7).endswith("…")
    assert _truncate_wordwise("hello", 10) == "hello"
    assert _truncate_wordwise("a b c d e f", 1) == "…"


def test_compose_caption_trim_advice_only():
    lines = [
        "История питания за 7 дней",
        "",
        "📊 Коротко о главном:",
        "🔥 Средние калории: 1234 ккал",
        "🥩 Средний белок: 100.0 г",
        "📅 Дней с записями: 7 из 7",
        "",
        "✨ Совет: " + ("слово " * 1000),
        "",
        "Хвост",
    ]
    cap = _compose_caption(lines, 1024)
    assert len(cap) <= 1024
    assert "✨ Совет:" in cap
    assert cap.startswith("История питания за 7 дней")
    assert cap.count("✨ Совет:") == 1


def test_compose_caption_no_trim_when_short():
    lines = ["x", "✨ Совет: коротко", "y"]
    cap = _compose_caption(lines, 1024)
    assert cap.endswith("y")
    assert "…" not in cap


def test_compose_caption_no_advice_truncates_whole():
    lines = ["".join(["line "] * 400)]
    cap = _compose_caption(lines, 1024)
    assert len(cap) <= 1024
