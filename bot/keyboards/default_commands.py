from __future__ import annotations
from typing import TYPE_CHECKING

from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeDefault

if TYPE_CHECKING:
    from aiogram import Bot

users_commands: dict[str, dict[str, str]] = {
    "en": {
        "start": "start bot",
        "templates": "meal templates",
        "day": "today stats",
        "history": "7-day nutrition history",
        "account": "account",
    },
    "uk": {
        "start": "start bot",
        "templates": "шаблони страв",
        "day": "статистика за сьогодні",
        "history": "історія харчування за 7 днів",
        "account": "особистий кабінет",
    },
    "ru": {
        "account": "💻 личный кабинет",
        "templates": "📌 шаблоны блюд",
        "day": "📈 статистика за сегодня",
        "history": "📅 питание за 7 дней",
        "add_meal": "❓ как добавлять блюда",
        "support": "☕️ поддержка",
        "start": "🚀 пересчитать план заново",
    },
}

admins_commands: dict[str, dict[str, str]] = {
    "en": {
        "ping": "Check bot ping",
        "stats": "Show bot stats",
    },
    "uk": {
        "ping": "Check bot ping",
        "stats": "Show bot stats",
    },
    "ru": {
        "ping": "Check bot ping",
        "stats": "Show bot stats",
    },
}


async def set_default_commands(bot: Bot) -> None:
    await remove_default_commands(bot)

    for language_code, commands in users_commands.items():
        await bot.set_my_commands(
            [BotCommand(command=command, description=description) for command, description in commands.items()],
            scope=BotCommandScopeDefault(),
            language_code=language_code,
        )
        await bot.set_my_commands(
            [BotCommand(command=command, description=description) for command, description in commands.items()],
            scope=BotCommandScopeAllPrivateChats(),
            language_code=language_code,
        )

        """ Commands for admins
        for admin_id in await admin_ids():
            await bot.set_my_commands(
                [
                    BotCommand(command=command, description=description)
                    for command, description in admins_commands[language_code].items()
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        """


async def remove_default_commands(bot: Bot) -> None:
    await bot.delete_my_commands(scope=BotCommandScopeDefault())
    await bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
