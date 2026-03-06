from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.i18n import gettext as _

_CATS = ("breakfast", "lunch", "dinner", "snack")


def _cat_label(cat: str) -> str:
    # Emojis per spec: 🥞 (breakfast), 🍜 (lunch), 🥗 (dinner), 🍎 (snack)
    if cat == "breakfast":
        return "🥞 " + _("Завтрак")
    if cat == "lunch":
        return "🍜 " + _("Обед")
    if cat == "dinner":
        return "🥗 " + _("Ужин")
    return "🍎 " + _("Перекус")


def categories_browse_kb(counts: dict[str, int] | None = None) -> InlineKeyboardMarkup:
    # Show counts like "🥞 Завтрак (3шт)" with 2-column layout
    buttons: list[InlineKeyboardButton] = []
    for cat in _CATS:
        base = _cat_label(cat)
        if counts and cat in counts:
            base = f"{base} ({int(counts.get(cat, 0))}шт)"
        buttons.append(InlineKeyboardButton(text=base, callback_data=f"tpl:cat:{cat}"))
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i:i+2])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def choose_category_kb(meal_id: int) -> InlineKeyboardMarkup:
    # 2 columns layout: left/right then next row
    buttons = [InlineKeyboardButton(text=_cat_label(cat), callback_data=f"tpl:save_menu:{meal_id}:{cat}") for cat in _CATS]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i:i+2])
    rows.append([InlineKeyboardButton(text=_("◀️Вернуться назад"), callback_data=f"tpl:save_back:{meal_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def templates_list_kb(templates: list[tuple[int, str]], category: str) -> InlineKeyboardMarkup:
    # Show rows with [➕ #i] and [❌Удалить]; no title button
    rows = []
    for idx, (tpl_id, _title) in enumerate(templates, start=1):
        rows.append([
            InlineKeyboardButton(text=f"➕ #{idx}", callback_data=f"tpl:add:{tpl_id}"),
            InlineKeyboardButton(text=_("❌Удалить"), callback_data=f"tpl:del:{tpl_id}:{category}"),
        ])
    rows.append([InlineKeyboardButton(text=_("◀️ Вернуться назад"), callback_data="tpl:back:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def save_confirm_kb(meal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_("✅Сохранить"), callback_data=f"tpl:save_go:{meal_id}")],
            [InlineKeyboardButton(text=_("◀️Вернуться назад"), callback_data=f"tpl:save_back:{meal_id}")],
        ]
    )
