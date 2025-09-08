#!/usr/bin/env python3
import asyncio
import json

from bot.services.foodai import analyze_text


async def main() -> None:
    text = "курица с рисом 200 г"
    res = await analyze_text(text)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
